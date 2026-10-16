import type { AgentCoreApplication } from '@aws/agentcore-cdk';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import type { Construct } from 'constructs';

/**
 * Fleet wiring: the environment variables and least-privilege IAM that `agentcore.json` cannot
 * express (design A10, spec fleet-operations).
 *
 * Environment variables carry parameter NAMES only. Each runtime reads the values from SSM at the
 * start of a match, so a recreated game stack is picked up without redeploying (no CDK-time
 * `valueForStringParameter`, no CloudFormation exports).
 *
 * Identity permissions (`GetResourceOauth2Token`, the client secret) are NOT granted here: the L3
 * construct already adds them to every runtime role whenever `credentials[]` is non-empty
 * (spikes/FINDINGS.md S3 criterion c). This module adds only SSM reads and the decider endpoint.
 *
 * It also PUBLISHES each runtime's ARN as a plain SSM String parameter (`/ctfagents/runtimes/<runtime>/arn`),
 * so a caller (the web lobby, `scripts/fleet.py`) can find the runtimes without parsing deployed-state or
 * CloudFormation. Plain parameters, not exports: nothing here creates a CloudFormation export or import.
 */

/** The slice of an AgentCore runtime this module needs. Tests record calls against it. */
export interface RuntimeEnvLike {
  readonly agentName: string;
  /** The runtime ARN (what `InvokeAgentRuntime.agentRuntimeArn` takes). A CloudFormation token at synth. */
  readonly runtimeArn: string;
  addEnvironmentVariable(key: string, value: string): void;
  addToRolePolicy(statement: iam.PolicyStatement): void;
}

export interface FleetMember {
  /** Runtime name in agentcore.json. */
  readonly runtime: string;
  /** OAuth credential name in agentcore.json (also the value of CTF_OAUTH_PROVIDER). */
  readonly credential: string;
  readonly usesDecider: boolean;
}

/** Order and names mirror agentcore.json (a jest test compares them). */
export const FLEET: readonly FleetMember[] = [
  { runtime: 'harness', credential: 'ctf_harness', usesDecider: false },
  { runtime: 'harness_sdk', credential: 'ctf_harness_sdk', usesDecider: false },
  { runtime: 'strand_decider', credential: 'ctf_strand_decider', usesDecider: true },
  { runtime: 'decider_loop', credential: 'ctf_decider_loop', usesDecider: true },
];

export const DEFAULT_MCP_URL_PARAM = '/agentic-ctf/mcp-url';
export const DEFAULT_DECIDER_ENDPOINT_PARAM = '/ctf-decider/endpoint-name';
export const DEFAULT_DECIDER_ENDPOINT_NAME = 'ctf-decider';
export const OAUTH_SCOPES = 'agentic-ctf/mcp';
export const AGENT_STATE_DIR = '/tmp/.agent';
/** `/ctfagents/runtimes/<runtime>/arn`: where each runtime ARN is published for callers. */
export const RUNTIME_ARN_PARAM_PREFIX = '/ctfagents/runtimes';

export interface FleetWiringProps {
  readonly account: string;
  readonly region: string;
  readonly partition?: string;
  readonly members?: readonly FleetMember[];
  readonly mcpUrlParam?: string;
  readonly deciderEndpointParam?: string;
  readonly deciderEndpointName?: string;
}

function ssmArn(partition: string, region: string, account: string, name: string): string {
  return `arn:${partition}:ssm:${region}:${account}:parameter/${name.replace(/^\//, '')}`;
}

function validate(envs: readonly RuntimeEnvLike[], members: readonly FleetMember[]): Map<string, RuntimeEnvLike> {
  const byName = new Map<string, RuntimeEnvLike>();
  for (const env of envs) {
    if (byName.has(env.agentName)) {
      throw new Error(`fleet wiring: duplicate environment for runtime ${env.agentName}`);
    }
    byName.set(env.agentName, env);
  }
  const known = new Set(members.map(m => m.runtime));
  const unknown = [...byName.keys()].filter(name => !known.has(name));
  if (unknown.length > 0) {
    throw new Error(
      `fleet wiring: runtime(s) not in the fleet table: ${unknown.join(', ')} (add them to FLEET or remove them from agentcore.json)`
    );
  }
  const missing = members.filter(m => !byName.has(m.runtime)).map(m => m.runtime);
  if (missing.length > 0) {
    throw new Error(`fleet wiring: no environment for fleet runtime(s): ${missing.join(', ')}`);
  }
  return byName;
}

/** The only code coupled to the alpha L3 construct: adapts its environments to `RuntimeEnvLike`. */
export function adaptEnvironments(app: AgentCoreApplication): RuntimeEnvLike[] {
  return Array.from(app.environments.values(), environment => ({
    agentName: environment.agent.name,
    runtimeArn: environment.runtime.runtimeArn,
    addEnvironmentVariable: (key: string, value: string) => environment.runtime.addEnvironmentVariable(key, value),
    addToRolePolicy: (statement: iam.PolicyStatement) => environment.runtime.addToPolicy(statement),
  }));
}

export function wireFleet(envs: Iterable<RuntimeEnvLike>, props: FleetWiringProps): void {
  const members = props.members ?? FLEET;
  const byName = validate([...envs], members); // validate everything before the first mutation
  const partition = props.partition ?? 'aws';
  const mcpUrlParam = props.mcpUrlParam ?? DEFAULT_MCP_URL_PARAM;
  const deciderEndpointParam = props.deciderEndpointParam ?? DEFAULT_DECIDER_ENDPOINT_PARAM;
  const endpointName = props.deciderEndpointName ?? DEFAULT_DECIDER_ENDPOINT_NAME;
  const arnOf = (name: string) => ssmArn(partition, props.region, props.account, name);

  for (const member of members) {
    const env = byName.get(member.runtime)!;
    env.addEnvironmentVariable('CTF_RUNTIME_NAME', member.runtime);
    env.addEnvironmentVariable('CTF_OAUTH_PROVIDER', member.credential);
    env.addEnvironmentVariable('CTF_OAUTH_SCOPES', OAUTH_SCOPES);
    env.addEnvironmentVariable('CTF_MCP_URL_PARAM', mcpUrlParam);
    env.addEnvironmentVariable('CTF_AGENT_STATE_DIR', AGENT_STATE_DIR);

    const parameters = [arnOf(mcpUrlParam)];
    if (member.usesDecider) {
      env.addEnvironmentVariable('CTF_DECIDER_ENDPOINT_PARAM', deciderEndpointParam);
      parameters.push(arnOf(deciderEndpointParam));
    }
    env.addToRolePolicy(new iam.PolicyStatement({ actions: ['ssm:GetParameter'], resources: parameters }));

    if (member.usesDecider) {
      env.addToRolePolicy(
        new iam.PolicyStatement({
          actions: ['sagemaker:InvokeEndpoint'],
          resources: [`arn:${partition}:sagemaker:${props.region}:${props.account}:endpoint/${endpointName}`],
        })
      );
    }
  }
}

export interface PublishRuntimeArnsProps {
  readonly members?: readonly FleetMember[];
  readonly prefix?: string;
}

export function runtimeArnParamName(runtime: string, prefix: string = RUNTIME_ARN_PARAM_PREFIX): string {
  return `${prefix.replace(/\/$/, '')}/${runtime}/arn`;
}

/** One plain String parameter per fleet runtime holding its runtime ARN. No exports, no ImportValue. */
export function publishRuntimeArns(
  scope: Construct,
  envs: Iterable<RuntimeEnvLike>,
  props: PublishRuntimeArnsProps = {}
): void {
  const members = props.members ?? FLEET;
  const byName = validate([...envs], members);
  for (const member of members) {
    new ssm.StringParameter(scope, `RuntimeArn${member.runtime.replace(/_/g, '')}`, {
      parameterName: runtimeArnParamName(member.runtime, props.prefix),
      stringValue: byName.get(member.runtime)!.runtimeArn,
      description: `Runtime ARN of the ${member.runtime} agent (InvokeAgentRuntime agentRuntimeArn)`,
    });
  }
}
