import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { DeciderStack } from '../lib/decider-stack';

const ACCOUNT = '000000000000';
const REGION = 'us-east-2';

function stackFor(props: { imageTag?: string; context?: Record<string, string> } = { imageTag: 'abc123' }) {
  const app = new cdk.App({ context: props.context });
  return new DeciderStack(app, 'TestDecider', {
    imageTag: props.imageTag,
    env: { account: ACCOUNT, region: REGION },
  });
}

function synth(props: { imageTag?: string; context?: Record<string, string> } = { imageTag: 'abc123' }): Template {
  return Template.fromStack(stackFor(props));
}

describe('endpoint', () => {
  const template = synth();

  test('has the fixed name ctf-decider', () => {
    template.hasResourceProperties('AWS::SageMaker::Endpoint', { EndpointName: 'ctf-decider' });
  });

  test('uses the generated endpoint config', () => {
    template.hasResourceProperties('AWS::SageMaker::Endpoint', {
      EndpointConfigName: Match.objectLike({ 'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('EndpointConfig')]) }),
    });
  });
});

describe('endpoint config', () => {
  const template = synth();

  test('runs one ml.g4dn.xlarge with a 600 s startup health check', () => {
    template.hasResourceProperties('AWS::SageMaker::EndpointConfig', {
      ProductionVariants: [
        Match.objectLike({
          InstanceType: 'ml.g4dn.xlarge',
          InitialInstanceCount: 1,
          InitialVariantWeight: 1,
          ContainerStartupHealthCheckTimeoutInSeconds: 600,
          VariantName: 'AllTraffic',
        }),
      ],
    });
  });

  test('pins the inference AMI (driver) and lets the context override it', () => {
    template.hasResourceProperties('AWS::SageMaker::EndpointConfig', {
      ProductionVariants: [Match.objectLike({ InferenceAmiVersion: 'al2-ami-sagemaker-inference-gpu-3-1' })],
    });
    synth({ imageTag: 'x', context: { inferenceAmiVersion: 'al2-ami-sagemaker-inference-gpu-2' } })
      .hasResourceProperties('AWS::SageMaker::EndpointConfig', {
        ProductionVariants: [Match.objectLike({ InferenceAmiVersion: 'al2-ami-sagemaker-inference-gpu-2' })],
      });
  });

  test('leaves the config name to CloudFormation (a change must replace it)', () => {
    const config = Object.values(template.findResources('AWS::SageMaker::EndpointConfig'))[0];
    expect(config.Properties.EndpointConfigName).toBeUndefined();
  });
});

describe('model', () => {
  const template = synth();

  test('leaves the model name to CloudFormation', () => {
    const model = Object.values(template.findResources('AWS::SageMaker::Model'))[0];
    expect(model.Properties.ModelName).toBeUndefined();
  });

  test('uses the image from the persistent repository, by tag', () => {
    const model = Object.values(template.findResources('AWS::SageMaker::Model'))[0];
    const image = JSON.stringify(model.Properties.PrimaryContainer.Image);
    expect(image).toContain('ctf-decider:abc123');
    expect(image).toContain('dkr.ecr');
  });

  test('is offline with fp16 by default and has no network', () => {
    template.hasResourceProperties('AWS::SageMaker::Model', {
      EnableNetworkIsolation: true,
      PrimaryContainer: Match.objectLike({
        Environment: Match.objectLike({ HF_HUB_OFFLINE: '1', DECIDER_DTYPE: 'float16' }),
      }),
    });
  });

  test('takes the dtype from context', () => {
    synth({ imageTag: 'x', context: { deciderDtype: 'float32' } }).hasResourceProperties('AWS::SageMaker::Model', {
      PrimaryContainer: Match.objectLike({ Environment: Match.objectLike({ DECIDER_DTYPE: 'float32' }) }),
    });
  });

  test('without an image tag the stack still synthesizes but carries a deploy-blocking error', () => {
    const stack = stackFor({});
    expect(() => Template.fromStack(stack)).not.toThrow();
    expect(Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('imageTag'))).toHaveLength(1);
  });

  test('reads the image tag from context', () => {
    const model = Object.values(
      synth({ context: { imageTag: 'from-context' } }).findResources('AWS::SageMaker::Model'),
    )[0];
    expect(JSON.stringify(model.Properties.PrimaryContainer.Image)).toContain('ctf-decider:from-context');
  });
});

describe('execution role', () => {
  const template = synth();

  test('is assumed by SageMaker only', () => {
    template.hasResourceProperties('AWS::IAM::Role', {
      AssumeRolePolicyDocument: {
        Statement: [Match.objectLike({ Principal: { Service: 'sagemaker.amazonaws.com' }, Action: 'sts:AssumeRole' })],
      },
    });
  });

  test('has no managed policies and no wildcard resource except the ECR auth token and the namespaced metrics', () => {
    const role = Object.values(template.findResources('AWS::IAM::Role'))[0];
    expect(role.Properties.ManagedPolicyArns).toBeUndefined();
    const statements = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    for (const statement of statements) {
      const actions = [statement.Action].flat() as string[];
      expect(actions.some((a) => a === '*' || a.endsWith(':*'))).toBe(false);
      if (statement.Resource === '*') {
        expect([['ecr:GetAuthorizationToken'], ['cloudwatch:PutMetricData']]).toContainEqual(actions);
      }
    }
  });

  test('may publish CloudWatch metrics only into the SageMaker endpoint namespace', () => {
    const statements = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    const metrics = statements.filter((s) => ([s.Action].flat() as string[]).includes('cloudwatch:PutMetricData'));
    expect(metrics).toHaveLength(1);
    expect(metrics[0].Condition).toEqual({
      StringEquals: { 'cloudwatch:namespace': '/aws/sagemaker/Endpoints' },
    });
    expect(metrics[0].Action).toEqual('cloudwatch:PutMetricData');
  });

  test('can only be assumed by SageMaker on behalf of this account (confused-deputy conditions)', () => {
    const role = Object.values(template.findResources('AWS::IAM::Role'))[0];
    const [trust] = role.Properties.AssumeRolePolicyDocument.Statement;
    expect(trust.Condition.StringEquals).toEqual({ 'aws:SourceAccount': ACCOUNT });
    // The ARN carries the partition as a token; only the account-and-region scoping is asserted.
    expect(JSON.stringify(trust.Condition.ArnLike['aws:SourceArn'])).toContain(`:sagemaker:${REGION}:${ACCOUNT}:*`);
    expect(Object.keys(trust.Condition.ArnLike)).toEqual(['aws:SourceArn']);
  });

  test('can pull from the repository and write only its own endpoint logs', () => {
    const statements = Object.values(template.findResources('AWS::IAM::Policy')).flatMap(
      (p) => p.Properties.PolicyDocument.Statement,
    );
    const actions = statements.flatMap((s) => [s.Action].flat() as string[]);
    expect(actions).toEqual(expect.arrayContaining(['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer']));
    const logs = statements.find((s) => ([s.Action].flat() as string[]).includes('logs:PutLogEvents'));
    expect(JSON.stringify(logs.Resource)).toContain('/aws/sagemaker/Endpoints/ctf-decider');
    expect(actions).not.toContain('ecr:PutImage');
  });
});

describe('ordering', () => {
  test('the model waits for the execution role policy (image pull must be allowed first)', () => {
    const template = synth();
    const model = Object.values(template.findResources('AWS::SageMaker::Model'))[0];
    expect(JSON.stringify(model.DependsOn)).toContain('ExecutionRoleDefaultPolicy');
  });
});

describe('publication and isolation from the persistent stack', () => {
  const template = synth();

  test('publishes the endpoint name to SSM', () => {
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/ctf-decider/endpoint-name',
      Type: 'String',
      Value: 'ctf-decider',
    });
  });

  test('has no Export in any Output', () => {
    const outputs = template.toJSON().Outputs ?? {};
    for (const output of Object.values(outputs) as { Export?: unknown }[]) {
      expect(output.Export).toBeUndefined();
    }
    expect(JSON.stringify(template.toJSON())).not.toContain('Fn::ImportValue');
  });

  test('has no Budget, ECR repository or CodeBuild project (those survive the teardown)', () => {
    for (const type of ['AWS::Budgets::Budget', 'AWS::ECR::Repository', 'AWS::CodeBuild::Project']) {
      expect(Object.keys(template.findResources(type))).toHaveLength(0);
    }
  });
});
