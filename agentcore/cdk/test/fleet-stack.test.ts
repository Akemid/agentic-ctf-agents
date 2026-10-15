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
  expect(exportNames.length).toBeGreaterThan(0);
  for (const name of exportNames) {
    expect(name).toMatch(/^FleetTest-[a-z-]+-(RuntimeId|RuntimeArn|RoleArn)$/);
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
