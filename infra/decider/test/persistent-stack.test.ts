import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { CtfPersistentStack } from '../lib/persistent-stack';

const EMAIL = 'budget-alerts@example.invalid';

function stackFor(props: { budgetEmail?: string } = { budgetEmail: EMAIL }): CtfPersistentStack {
  const app = new cdk.App();
  return new CtfPersistentStack(app, 'TestPersistent', {
    budgetEmail: props.budgetEmail,
    modelId: 'Org/some-model',
  });
}

function synth(props: { budgetEmail?: string } = { budgetEmail: EMAIL }): Template {
  return Template.fromStack(stackFor(props));
}

function buildSpec(template: Template): string {
  const project = Object.values(template.findResources('AWS::CodeBuild::Project'))[0];
  return project.Properties.Source.BuildSpec;
}

describe('ECR repository', () => {
  const template = synth();

  test('is named ctf-decider, immutable, scanned on push', () => {
    template.hasResourceProperties('AWS::ECR::Repository', {
      RepositoryName: 'ctf-decider',
      ImageTagMutability: 'IMMUTABLE',
      ImageScanningConfiguration: { ScanOnPush: true },
    });
  });

  test('survives stack deletion (RETAIN)', () => {
    template.hasResource('AWS::ECR::Repository', {
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });

  test('keeps only the last 3 images', () => {
    const repo = Object.values(template.findResources('AWS::ECR::Repository'))[0];
    const policy = JSON.parse(repo.Properties.LifecyclePolicy.LifecyclePolicyText);
    expect(policy.rules).toHaveLength(1);
    expect(policy.rules[0].selection).toMatchObject({ countType: 'imageCountMoreThan', countNumber: 3 });
    expect(policy.rules[0].action).toEqual({ type: 'expire' });
  });
});

describe('Budget', () => {
  const template = synth();

  test('is a monthly 25 USD cost budget filtered to SageMaker', () => {
    template.hasResourceProperties('AWS::Budgets::Budget', {
      Budget: {
        BudgetType: 'COST',
        TimeUnit: 'MONTHLY',
        BudgetLimit: { Amount: 25, Unit: 'USD' },
        CostFilters: { Service: ['Amazon SageMaker'] },
      },
    });
  });

  test('alerts at 80 percent actual and 100 percent forecast, by email', () => {
    template.hasResourceProperties('AWS::Budgets::Budget', {
      NotificationsWithSubscribers: Match.arrayEquals([
        {
          Notification: {
            NotificationType: 'ACTUAL',
            ComparisonOperator: 'GREATER_THAN',
            Threshold: 80,
            ThresholdType: 'PERCENTAGE',
          },
          Subscribers: [{ SubscriptionType: 'EMAIL', Address: EMAIL }],
        },
        {
          Notification: {
            NotificationType: 'FORECASTED',
            ComparisonOperator: 'GREATER_THAN',
            Threshold: 100,
            ThresholdType: 'PERCENTAGE',
          },
          Subscribers: [{ SubscriptionType: 'EMAIL', Address: EMAIL }],
        },
      ]),
    });
  });

  test('without an email it still synthesizes (so it can be destroyed) but cannot be deployed', () => {
    const saved = process.env.CTF_BUDGET_EMAIL;
    delete process.env.CTF_BUDGET_EMAIL;
    try {
      const stack = stackFor({});
      expect(() => Template.fromStack(stack)).not.toThrow();
      expect(Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('budgetEmail'))).toHaveLength(1);
    } finally {
      if (saved !== undefined) process.env.CTF_BUDGET_EMAIL = saved;
    }
  });

  test('takes the email from CTF_BUDGET_EMAIL when no prop is given', () => {
    const saved = process.env.CTF_BUDGET_EMAIL;
    process.env.CTF_BUDGET_EMAIL = 'from-env@example.invalid';
    try {
      synth({}).hasResourceProperties('AWS::Budgets::Budget', {
        NotificationsWithSubscribers: Match.arrayWith([
          Match.objectLike({ Subscribers: [{ SubscriptionType: 'EMAIL', Address: 'from-env@example.invalid' }] }),
        ]),
      });
    } finally {
      if (saved === undefined) delete process.env.CTF_BUDGET_EMAIL;
      else process.env.CTF_BUDGET_EMAIL = saved;
    }
  });
});

describe('CodeBuild image builder', () => {
  const template = synth();

  test('is a privileged amd64 Linux build with an S3 source', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Environment: Match.objectLike({
        Type: 'LINUX_CONTAINER',
        PrivilegedMode: true,
        ComputeType: 'BUILD_GENERAL1_LARGE',
      }),
      Source: Match.objectLike({ Type: 'S3' }),
    });
  });

  test('receives the model id and repository as environment variables', () => {
    template.hasResourceProperties('AWS::CodeBuild::Project', {
      Environment: Match.objectLike({
        EnvironmentVariables: Match.arrayWith([
          Match.objectLike({ Name: 'MODEL_ID', Value: 'Org/some-model' }),
          Match.objectLike({ Name: 'REPOSITORY_NAME', Value: 'ctf-decider' }),
        ]),
      }),
    });
  });

  test('buildspec builds for amd64, checks the source hash, and pushes the tag', () => {
    const spec = buildSpec(template);
    expect(spec).toContain('--platform linux/amd64');
    expect(spec).toContain('hash_source.py');
    expect(spec).toContain('docker push');
    expect(spec).toContain('$IMAGE_TAG');
  });

  test('post_build pushes only when the build succeeded (post_build also runs after a failed build)', () => {
    const spec = buildSpec(template);
    expect(spec).toMatch(/\[ \\?"\$CODEBUILD_BUILD_SUCCEEDING\\?" = 1 \]/);
    const guard = spec.indexOf('CODEBUILD_BUILD_SUCCEEDING');
    expect(guard).toBeGreaterThan(-1);
    expect(guard).toBeLessThan(spec.indexOf('docker push'));
  });

  test('the Dockerfile base is the ECR Public python image, pinned by tag and digest', () => {
    const dockerfile = fs.readFileSync(path.resolve(__dirname, '..', 'adapter', 'Dockerfile'), 'utf8');
    const from = dockerfile.split('\n').find((line) => line.startsWith('FROM '))!;
    expect(from).toMatch(/ public\.ecr\.aws\/docker\/library\/python:3\.\d+\.\d+-slim-\w+@sha256:[0-9a-f]{64}$/);
  });

  test('the buildspec logs in to our own registry only, never to a third-party one', () => {
    const spec = buildSpec(template);
    const logins = spec.match(/docker login [^,\n]*/g) ?? [];
    expect(logins).toHaveLength(1);
    expect(logins[0]).toContain('REPOSITORY_URI');
    expect(spec).not.toMatch(/\d{12}\.dkr\.ecr/);
    expect(spec).not.toContain('get-login-password --region');
  });

  test('its role has no pull grant on any repository outside this stack, only the auth token', () => {
    const statements = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    expect(JSON.stringify(statements)).not.toMatch(/arn:aws:ecr:[^"]*:\d{12}:repository/);
    const token = statements.filter((s) => [s.Action].flat().includes('ecr:GetAuthorizationToken'));
    expect(token.length).toBeGreaterThan(0);
    expect(token.every((s) => s.Resource === '*')).toBe(true);
  });

  test('its role can push to the repository only, plus the auth token', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const statements = policies.flatMap((p) => p.Properties.PolicyDocument.Statement);
    const ecr = statements.filter((s: { Action: string | string[] }) =>
      [s.Action].flat().some((a: string) => a.startsWith('ecr:')),
    );
    const push = ecr.find((s: { Action: string | string[] }) => [s.Action].flat().includes('ecr:PutImage'));
    expect(push).toBeDefined();
    expect(push.Resource).not.toBe('*');
    const token = ecr.find((s: { Action: string | string[] }) =>
      [s.Action].flat().includes('ecr:GetAuthorizationToken'),
    );
    expect(token.Resource).toBe('*');
  });
});

describe('SSM and outputs', () => {
  const template = synth();

  test('publishes the repository name, URI and build project name', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', { Name: '/ctf-decider/repo-uri', Type: 'String' });
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/ctf-decider/build-project',
      Type: 'String',
    });
  });

  test('publishes the fixed endpoint name, with no dependency on any endpoint', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/ctf-decider/endpoint-name',
      Type: 'String',
      Value: 'ctf-decider',
    });
    const params = Object.values(template.findResources('AWS::SSM::Parameter')) as {
      Properties: { Name: string };
      DependsOn?: unknown;
    }[];
    const param = params.find((p) => p.Properties.Name === '/ctf-decider/endpoint-name');
    expect(param?.DependsOn).toBeUndefined();
  });

  test('exports nothing', () => {
    const outputs = template.toJSON().Outputs ?? {};
    for (const output of Object.values(outputs) as { Export?: unknown }[]) {
      expect(output.Export).toBeUndefined();
    }
  });

  test('contains no SageMaker resources (those belong to DeciderStack)', () => {
    expect(Object.keys(template.findResources('AWS::SageMaker::Endpoint'))).toHaveLength(0);
  });
});
