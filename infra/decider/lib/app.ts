import type { App, Stack } from 'aws-cdk-lib';
import { DeciderStack } from './decider-stack';
import { CtfPersistentStack } from './persistent-stack';

export interface BuildOptions {
  readonly env?: NodeJS.ProcessEnv;
}

/**
 * `cdk deploy|destroy <name>` runs the whole app without telling it which stack was asked for, and each
 * stack needs a different input only to be DEPLOYED (budget email once, image tag weekly). Tearing the
 * endpoint down must never depend on remembering a tag, so both stacks are always built:
 *
 * - a stack whose input is missing carries an error annotation (see each stack), which makes
 *   `cdk deploy` and `cdk synth <stack>` fail, while `cdk destroy` does not validate annotations;
 * - the placeholder it falls back to is inert (an unusable image tag, an `.invalid` address).
 */
export function buildStacks(app: App, options: BuildOptions = {}): Stack[] {
  const processEnv = options.env ?? process.env;
  const awsEnv = { account: processEnv.CDK_DEFAULT_ACCOUNT, region: processEnv.CDK_DEFAULT_REGION };
  const budgetEmail = app.node.tryGetContext('budgetEmail') || processEnv.CTF_BUDGET_EMAIL || undefined;
  const imageTag = app.node.tryGetContext('imageTag') || undefined;

  return [
    new CtfPersistentStack(app, 'CtfPersistentStack', { env: awsEnv, budgetEmail }),
    new DeciderStack(app, 'DeciderStack', { env: awsEnv, imageTag }),
  ];
}
