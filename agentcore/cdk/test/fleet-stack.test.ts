import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AgentCoreStack } from '../lib/cdk-stack';

/**
 * A real `AgentCoreStack` synthesized from the committed agentcore.json. Code packaging (uv + pip,
 * about two minutes for four runtimes) is the only part replaced: the L3 packager is stubbed to
 * return a tiny zip, because it is not CDK bundling and no CDK context can skip it.
 */
// The package `exports` map hides the packager, so it is loaded by file path (same module instance
// the L3 constructs use).
const { PythonBundledCodeZipAsset } = require(
  path.join(
    path.dirname(require.resolve('@aws/agentcore-cdk')),
    'cdk/constructs/bundling/zip/PythonBundledCodeZipAsset.js'
  )
);
const spec = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'agentcore.json'), 'utf8'));
const RUNTIMES = ['harness', 'harness_sdk', 'strand_decider', 'decider_loop'];
const DECIDER = ['strand_decider', 'decider_loop'];

let template: Template;
let tmp: string;

beforeAll(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-stack-'));
  const zip = path.join(tmp, 'code.zip');
  fs.writeFileSync(zip, 'PK-not-a-real-zip');
  jest.spyOn(PythonBundledCodeZipAsset.prototype, 'packageCode').mockReturnValue({ artifactPath: zip, sizeBytes: 17 });
  const app = new cdk.App();
  const stack = new AgentCoreStack(app, 'FleetTest', {
    spec,
    env: { account: '000000000000', region: 'us-east-2' },
  });
  template = Template.fromStack(stack);
});

afterAll(() => {
  jest.restoreAllMocks();
  fs.rmSync(tmp, { recursive: true, force: true });
});

function runtimeEnv(name: string): Record<string, unknown> {
  const matches = Object.values(template.findResources('AWS::BedrockAgentCore::Runtime')).filter(r =>
    String(r.Properties.AgentRuntimeName).includes(name)
  );
  const exact = matches.filter(r => String(r.Properties.AgentRuntimeName).endsWith(`_${name}`));
  expect(exact).toHaveLength(1);
  return exact[0].Properties.EnvironmentVariables ?? {};
}

test('synthesizes one runtime per fleet member', () => {
  template.resourceCountIs('AWS::BedrockAgentCore::Runtime', RUNTIMES.length);
});

test.each(RUNTIMES)('%s carries its own provider and parameter names', name => {
  const env = runtimeEnv(name);
  expect(env).toMatchObject({
    CTF_RUNTIME_NAME: name,
    CTF_OAUTH_PROVIDER: `ctf_${name}`,
    CTF_MCP_URL_PARAM: '/agentic-ctf/mcp-url',
  });
  expect('CTF_DECIDER_ENDPOINT_PARAM' in env).toBe(DECIDER.includes(name));
});

test('exports are only the alpha L3 ones and nothing is imported; wiring never reads SSM at deploy time', () => {
  const json = template.toJSON();
  const exportNames = Object.values(json.Outputs ?? {})
    .map((o: any) => o.Export?.Name as string | undefined)
    .filter((n): n is string => n !== undefined);
  // The L3's own export set is exactly {RuntimeId, RuntimeArn, RoleArn} per runtime: this project adds none.
  const l3Exports = RUNTIMES.flatMap(n => ['RuntimeId', 'RuntimeArn', 'RoleArn'].map(k => `FleetTest-${n.replace(/_/g, '-')}-${k}`));
  expect([...exportNames].sort()).toEqual([...l3Exports].sort());
  // ... and the new SSM parameter resources produced no Output at all (exported or not).
  const ssmIds = Object.keys(template.findResources('AWS::SSM::Parameter'));
  expect(ssmIds).toHaveLength(RUNTIMES.length);
  for (const output of Object.values(json.Outputs ?? {})) {
    for (const id of ssmIds) {
      expect(JSON.stringify(output)).not.toContain(id);
    }
  }
  expect(JSON.stringify(json)).not.toContain('Fn::ImportValue');
  const ssmParameters = Object.entries(json.Parameters ?? {}).filter(
    ([name, p]: [string, any]) => name !== 'BootstrapVersion' && String(p.Type).startsWith('AWS::SSM::Parameter::Value')
  );
  expect(ssmParameters).toEqual([]);
});

test('the decider SSM grant names the endpoint parameter, never a wildcard', () => {
  const statements = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(p =>
    [p.Properties.PolicyDocument.Statement].flat()
  );
  const ssm = statements.filter(s => [s.Action].flat().includes('ssm:GetParameter'));
  const resources = JSON.stringify(ssm.map(s => s.Resource));
  expect(resources).toContain('parameter/ctf-decider/endpoint-name');
  expect(resources).not.toContain('ctf-decider/*');
});

test('publishes the four runtime ARNs as plain String parameters, with no new exports or imports', () => {
  const params = Object.values(template.findResources('AWS::SSM::Parameter'));
  expect(params).toHaveLength(RUNTIMES.length);
  const byName = Object.fromEntries(params.map((p: any) => [p.Properties.Name, p.Properties]));
  expect(Object.keys(byName).sort()).toEqual(RUNTIMES.map(n => `/ctfagents/runtimes/${n}/arn`).sort());
  for (const name of RUNTIMES) {
    const props = byName[`/ctfagents/runtimes/${name}/arn`];
    expect(props.Type).toBe('String');
    // the value is a token that resolves to the runtime's own ARN attribute, never a literal
    expect(JSON.stringify(props.Value)).toContain('AgentRuntimeArn');
  }
  const outputs = Object.values(template.toJSON().Outputs ?? {}) as any[];
  expect(outputs.filter(o => String(o.Value && JSON.stringify(o.Value)).includes('ctfagents'))).toEqual([]);
  expect(JSON.stringify(template.toJSON())).not.toContain('Fn::ImportValue');
});

test('the runtime roles are not granted anything on the published ARN parameters', () => {
  const statements = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(p =>
    [p.Properties.PolicyDocument.Statement].flat()
  );
  expect(JSON.stringify(statements.map(s => s.Resource))).not.toContain('ctfagents');
});
