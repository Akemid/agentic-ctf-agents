import * as cdk from 'aws-cdk-lib';
import { Annotations, Match } from 'aws-cdk-lib/assertions';
import { buildStacks } from '../lib/app';
import { DeciderStack, PLACEHOLDER_IMAGE_TAG } from '../lib/decider-stack';
import { CtfPersistentStack } from '../lib/persistent-stack';

const EMAIL = 'a@example.invalid';

function build(context: Record<string, string> = {}, env: NodeJS.ProcessEnv = {}) {
  const app = new cdk.App({ context });
  const stacks = buildStacks(app, { env });
  const decider = stacks.find((s): s is DeciderStack => s instanceof DeciderStack)!;
  const persistent = stacks.find((s): s is CtfPersistentStack => s instanceof CtfPersistentStack)!;
  return { app, stacks, decider, persistent };
}

const errors = (stack: cdk.Stack) => Annotations.fromStack(stack).findError('*', Match.anyValue());

describe('buildStacks', () => {
  test('always builds both stacks, with no context at all (so `cdk destroy` needs no inputs)', () => {
    const { stacks, app } = build();
    expect(stacks.map((s) => s.stackName)).toEqual(['CtfPersistentStack', 'DeciderStack']);
    expect(() => app.synth()).not.toThrow();
  });

  test('builds both when every input is present', () => {
    const { stacks, decider, persistent } = build({ imageTag: 'abc', budgetEmail: EMAIL });
    expect(stacks).toHaveLength(2);
    expect(errors(decider)).toHaveLength(0);
    expect(errors(persistent)).toHaveLength(0);
  });

  test('reads the budget email from CTF_BUDGET_EMAIL', () => {
    const { persistent } = build({}, { CTF_BUDGET_EMAIL: EMAIL });
    expect(errors(persistent)).toHaveLength(0);
  });
});

describe('deploy guards (an error annotation makes `cdk deploy` and `cdk synth <stack>` fail, destroy ignores it)', () => {
  test('DeciderStack without imageTag is blocked, with a message that says how to fix it', () => {
    const { decider } = build({ budgetEmail: EMAIL });
    const found = Annotations.fromStack(decider).findError('*', Match.stringLikeRegexp('imageTag'));
    expect(found).toHaveLength(1);
    expect(String(found[0].entry.data)).toMatch(/-c imageTag=/);
  });

  test('the placeholder image tag is what a blocked DeciderStack carries, never a real-looking tag', () => {
    const { decider } = build();
    const json = JSON.stringify(cdk.assertions.Template.fromStack(decider).toJSON());
    expect(json).toContain(`ctf-decider:${PLACEHOLDER_IMAGE_TAG}`);
    expect(PLACEHOLDER_IMAGE_TAG).toMatch(/placeholder/i);
  });

  test('an empty imageTag is treated as missing', () => {
    const { decider } = build({ imageTag: '' });
    expect(errors(decider)).toHaveLength(1);
  });

  test('CtfPersistentStack without a budget email is blocked, DeciderStack is not affected', () => {
    const { persistent, decider } = build({ imageTag: 'abc' });
    expect(Annotations.fromStack(persistent).findError('*', Match.stringLikeRegexp('budgetEmail'))).toHaveLength(1);
    expect(errors(decider)).toHaveLength(0);
  });

  test('the placeholder email never looks like a real address', () => {
    const { persistent } = build();
    const json = JSON.stringify(cdk.assertions.Template.fromStack(persistent).toJSON());
    expect(json).toMatch(/placeholder@example\.invalid/);
  });
});
