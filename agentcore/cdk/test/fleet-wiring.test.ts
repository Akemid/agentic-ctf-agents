import * as fs from 'fs';
import * as path from 'path';
import { Stack } from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as iam from 'aws-cdk-lib/aws-iam';
import { adaptEnvironments, FLEET, publishRuntimeArns, wireFleet, type RuntimeEnvLike } from '../lib/fleet-wiring';

const ACCOUNT = '000000000000';
const REGION = 'us-east-2';
const PROPS = { account: ACCOUNT, region: REGION };

class RecordingEnv implements RuntimeEnvLike {
  readonly env: Record<string, string> = {};
  readonly statements: Array<ReturnType<iam.PolicyStatement['toStatementJson']>> = [];
  readonly runtimeArn: string;
  constructor(readonly agentName: string) {
    this.runtimeArn = `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${agentName}-AbC123`;
  }
  addEnvironmentVariable(key: string, value: string): void {
    this.env[key] = value;
  }
  addToRolePolicy(statement: iam.PolicyStatement): void {
    this.statements.push(statement.toStatementJson());
  }
  actions(): string[] {
    return this.statements.flatMap(s => [s.Action].flat());
  }
  resourcesFor(action: string): string[] {
    return this.statements.filter(s => [s.Action].flat().includes(action)).flatMap(s => [s.Resource].flat());
  }
}

const NAMES = ['harness', 'harness_sdk', 'strand_decider', 'decider_loop'];
const DECIDER_NAMES = ['strand_decider', 'decider_loop'];
const PLAIN_NAMES = ['harness', 'harness_sdk'];

function wired(extra: Partial<typeof PROPS> & Record<string, unknown> = {}): Record<string, RecordingEnv> {
  const envs = NAMES.map(n => new RecordingEnv(n));
  wireFleet(envs, { ...PROPS, ...extra });
  return Object.fromEntries(envs.map(e => [e.agentName, e]));
}

describe('FLEET table', () => {
  const spec = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'agentcore.json'), 'utf8'));

  test('lists exactly the runtimes of agentcore.json, in order', () => {
    expect(FLEET.map(m => m.runtime)).toEqual(spec.runtimes.map((r: { name: string }) => r.name));
  });

  test('credential names equal the OAuth credentials of agentcore.json', () => {
    expect(FLEET.map(m => m.credential)).toEqual(spec.credentials.map((c: { name: string }) => c.name));
  });

  test('only the two decider runtimes use the decider', () => {
    expect(FLEET.filter(m => m.usesDecider).map(m => m.runtime)).toEqual(DECIDER_NAMES);
  });
});

describe('wireFleet environment variables', () => {
  test.each(NAMES)('%s gets the common variables with its own provider', name => {
    const env = wired()[name].env;
    expect(env.CTF_RUNTIME_NAME).toBe(name);
    expect(env.CTF_OAUTH_PROVIDER).toBe(`ctf_${name}`);
    expect(env.CTF_OAUTH_SCOPES).toBe('agentic-ctf/mcp');
    expect(env.CTF_MCP_URL_PARAM).toBe('/agentic-ctf/mcp-url');
    expect(env.CTF_AGENT_STATE_DIR).toBe('/tmp/.agent');
  });

  test('providers are four distinct names', () => {
    const providers = Object.values(wired()).map(e => e.env.CTF_OAUTH_PROVIDER);
    expect(new Set(providers).size).toBe(4);
  });

  test.each(DECIDER_NAMES)('%s also gets the decider endpoint parameter name', name => {
    expect(wired()[name].env.CTF_DECIDER_ENDPOINT_PARAM).toBe('/ctf-decider/endpoint-name');
  });

  test.each(PLAIN_NAMES)('%s does not get the decider variable', name => {
    expect(wired()[name].env).not.toHaveProperty('CTF_DECIDER_ENDPOINT_PARAM');
  });

  test('parameter names can be overridden', () => {
    const env = wired({ mcpUrlParam: '/other/mcp', deciderEndpointParam: '/other/endpoint' });
    expect(env.harness.env.CTF_MCP_URL_PARAM).toBe('/other/mcp');
    expect(env.decider_loop.env.CTF_DECIDER_ENDPOINT_PARAM).toBe('/other/endpoint');
  });

  test('the variables carry parameter names only, never values or secrets', () => {
    for (const e of Object.values(wired())) {
      for (const value of Object.values(e.env)) {
        expect(value).not.toMatch(/^https?:/);
        expect(value).not.toMatch(/arn:aws:secretsmanager/);
      }
    }
  });
});

describe('wireFleet IAM', () => {
  const mcpArn = `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/agentic-ctf/mcp-url`;

  test.each(NAMES)('%s can read the MCP url parameter and nothing else of agentic-ctf', name => {
    const e = wired()[name];
    expect(e.resourcesFor('ssm:GetParameter')).toContain(mcpArn);
    expect(e.resourcesFor('ssm:GetParameter').filter(r => r.includes('agentic-ctf'))).toEqual([mcpArn]);
  });

  test.each(DECIDER_NAMES)(
    '%s may read only the endpoint-name parameter and invoke only the ctf-decider endpoint',
    name => {
      const e = wired()[name];
      const endpointArn = `arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/ctf-decider/endpoint-name`;
      expect(e.resourcesFor('ssm:GetParameter')).toEqual([mcpArn, endpointArn]);
      expect(e.resourcesFor('ssm:GetParameter').filter(r => r.includes('*'))).toEqual([]);
      expect(e.resourcesFor('sagemaker:InvokeEndpoint')).toEqual([
        `arn:aws:sagemaker:${REGION}:${ACCOUNT}:endpoint/ctf-decider`,
      ]);
    }
  );

  test.each(PLAIN_NAMES)('%s has no decider grants', name => {
    const e = wired()[name];
    expect(e.actions()).toEqual(['ssm:GetParameter']);
    expect(e.resourcesFor('ssm:GetParameter')).toEqual([mcpArn]);
  });

  test('SSM access is read-only and no runtime is granted the agent client secrets', () => {
    for (const e of Object.values(wired())) {
      for (const action of e.actions()) {
        expect(['ssm:GetParameter', 'sagemaker:InvokeEndpoint']).toContain(action);
      }
      const resources = e.statements.flatMap(s => [s.Resource].flat());
      expect(resources.filter(r => /agents\/|secretsmanager/.test(r))).toEqual([]);
    }
  });

  test('never re-grants the Identity permissions that the L3 construct already adds', () => {
    for (const e of Object.values(wired())) {
      expect(e.actions().filter(a => /bedrock-agentcore|secretsmanager|sts:/.test(a))).toEqual([]);
    }
  });

  test('a custom endpoint parameter name is granted exactly, not its parent path', () => {
    const e = wired({ deciderEndpointParam: '/other/endpoint' }).decider_loop;
    expect(e.resourcesFor('ssm:GetParameter')).toContain(`arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/other/endpoint`);
    expect(e.resourcesFor('ssm:GetParameter').filter(r => r.includes('*'))).toEqual([]);
  });

  test('uses the given partition and a custom decider endpoint name', () => {
    const e = wired({ partition: 'aws-cn', deciderEndpointName: 'my-endpoint' }).decider_loop;
    expect(e.resourcesFor('sagemaker:InvokeEndpoint')).toEqual([
      `arn:aws-cn:sagemaker:${REGION}:${ACCOUNT}:endpoint/my-endpoint`,
    ]);
  });
});

describe('wireFleet validation', () => {
  test('throws naming the runtime when a fleet member has no environment', () => {
    const envs = ['harness', 'harness_sdk', 'strand_decider'].map(n => new RecordingEnv(n));
    expect(() => wireFleet(envs, PROPS)).toThrow(/decider_loop/);
  });

  test('throws naming the runtime when the app has an unknown environment', () => {
    const envs = [...NAMES, 'surprise'].map(n => new RecordingEnv(n));
    expect(() => wireFleet(envs, PROPS)).toThrow(/surprise/);
  });

  test('records nothing before it validates', () => {
    const envs = ['harness'].map(n => new RecordingEnv(n));
    expect(() => wireFleet(envs, PROPS)).toThrow();
    expect(envs[0].env).toEqual({});
    expect(envs[0].statements).toEqual([]);
  });

  test('rejects duplicate environments for one runtime', () => {
    const envs = [...NAMES, 'harness'].map(n => new RecordingEnv(n));
    expect(() => wireFleet(envs, PROPS)).toThrow(/duplicate.*harness/i);
  });

  test('members can be narrowed (a deploy of a subset)', () => {
    const only = [FLEET[1]];
    const env = new RecordingEnv('harness_sdk');
    wireFleet([env], { ...PROPS, members: only });
    expect(env.env.CTF_OAUTH_PROVIDER).toBe('ctf_harness_sdk');
  });
});

describe('adaptEnvironments', () => {
  function fakeApp() {
    const calls: Array<[string, string, string]> = [];
    const policies: Array<[string, iam.PolicyStatement]> = [];
    const environments = new Map(
      NAMES.map(n => [
        n,
        {
          agent: { name: n },
          runtime: {
            runtimeArn: `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${n}-XyZ789`,
            addEnvironmentVariable: (k: string, v: string) => calls.push([n, k, v]),
            addToPolicy: (s: iam.PolicyStatement) => policies.push([n, s]),
          },
        },
      ])
    );
    return { app: { environments } as unknown as Parameters<typeof adaptEnvironments>[0], calls, policies };
  }

  test('exposes one RuntimeEnvLike per environment, named by the agent', () => {
    const { app } = fakeApp();
    expect(adaptEnvironments(app).map(e => e.agentName)).toEqual(NAMES);
  });

  test('maps the L3 runtime ARN of each environment', () => {
    const { app } = fakeApp();
    expect(adaptEnvironments(app).map(e => e.runtimeArn)).toEqual(
      NAMES.map(n => `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${n}-XyZ789`)
    );
  });

  test('forwards variables and statements to the right runtime', () => {
    const { app, calls, policies } = fakeApp();
    wireFleet(adaptEnvironments(app), PROPS);
    expect(calls.filter(([n, k]) => n === 'decider_loop' && k === 'CTF_OAUTH_PROVIDER')).toEqual([
      ['decider_loop', 'CTF_OAUTH_PROVIDER', 'ctf_decider_loop'],
    ]);
    expect(policies.filter(([n]) => n === 'harness')).toHaveLength(1);
    expect(policies.filter(([n]) => n === 'strand_decider')).toHaveLength(2);
  });
});

describe('publishRuntimeArns', () => {
  function publish(members?: typeof FLEET) {
    const stack = new Stack(undefined, 'Pub', { env: { account: ACCOUNT, region: REGION } });
    const envs = (members ? members.map(m => m.runtime) : NAMES).map(n => new RecordingEnv(n));
    publishRuntimeArns(stack, envs, members ? { members } : undefined);
    return { stack, template: Template.fromStack(stack), envs };
  }

  test('creates one String parameter per runtime under /ctfagents/runtimes/<runtime>/arn', () => {
    const { template } = publish();
    template.resourceCountIs('AWS::SSM::Parameter', 4);
    for (const name of NAMES) {
      template.hasResourceProperties('AWS::SSM::Parameter', {
        Name: `/ctfagents/runtimes/${name}/arn`,
        Type: 'String',
        Value: `arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${name}-AbC123`,
      });
    }
  });

  test('the value is the runtime ARN itself (what InvokeAgentRuntime takes), not an id or an endpoint', () => {
    const { template } = publish();
    for (const p of Object.values(template.findResources('AWS::SSM::Parameter'))) {
      expect(String(p.Properties.Value)).toMatch(/^arn:aws:bedrock-agentcore:[^:]+:\d{12}:runtime\/[^/]+$/);
    }
  });

  test('adds no outputs, exports or imports', () => {
    const { template } = publish();
    const json = template.toJSON();
    expect(json.Outputs ?? {}).toEqual({});
    expect(JSON.stringify(json)).not.toContain('Fn::ImportValue');
  });

  test('narrowed members publish only their own runtime', () => {
    const { template } = publish([FLEET[1]]);
    template.resourceCountIs('AWS::SSM::Parameter', 1);
    template.hasResourceProperties('AWS::SSM::Parameter', { Name: '/ctfagents/runtimes/harness_sdk/arn' });
  });

  test('validates like wireFleet: an environment outside the fleet table is an error', () => {
    const stack = new Stack(undefined, 'Bad');
    const envs = [...NAMES, 'surprise'].map(n => new RecordingEnv(n));
    expect(() => publishRuntimeArns(stack, envs)).toThrow(/surprise/);
  });
});
