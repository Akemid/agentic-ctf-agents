import { Annotations, ArnFormat, Stack, type StackProps } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sagemaker from 'aws-cdk-lib/aws-sagemaker';
import type { Construct } from 'constructs';
import { ENDPOINT_NAME, REPOSITORY_NAME } from './names';

export interface DeciderStackProps extends StackProps {
  /** Image tag in the persistent ECR repository. Falls back to context `imageTag`. */
  readonly imageTag?: string;
}

/**
 * What a DeciderStack built WITHOUT an image tag points its model at. It exists so the stack can always be
 * synthesized (`cdk destroy DeciderStack` needs no inputs), and it is useless on purpose: a stack in this
 * state also carries an error annotation, so `cdk deploy` refuses to run (`cdk destroy` does not validate).
 */
export const PLACEHOLDER_IMAGE_TAG = 'placeholder-imagetag-required';
/** The namespace SageMaker endpoint instance metrics (CPU, memory, GPU) are published under. */
export const ENDPOINT_METRICS_NAMESPACE = '/aws/sagemaker/Endpoints';

export const INSTANCE_TYPE = 'ml.g4dn.xlarge';
// A SageMaker endpoint cannot be deleted while it is `Creating`, so a broken container is billed for this whole
// timeout. Loading the 2B model (baked into the image) plus the warm-up takes about 2-4 minutes.
export const STARTUP_HEALTH_CHECK_SECONDS = 600;
const DEFAULT_DTYPE = 'float16';
// Driver 550 / CUDA 12.4. A CUDA 12.x container needs a driver >= 525. [UNVERIFIED on g4dn until the first deploy]
const DEFAULT_INFERENCE_AMI = 'al2-ami-sagemaker-inference-gpu-3-1';

/**
 * Weekly stack: one GPU endpoint. Everything that must outlive it (image repository, budget)
 * lives in CtfPersistentStack, including the `/ctf-decider/endpoint-name` parameter: it must exist
 * whether or not this stack does (the decider is optional). Nothing is exported.
 */
export class DeciderStack extends Stack {
  constructor(scope: Construct, id: string, props: DeciderStackProps = {}) {
    super(scope, id, props);

    let imageTag: string | undefined = props.imageTag || this.node.tryGetContext('imageTag') || undefined;
    if (!imageTag) {
      Annotations.of(this).addError(
        'An image tag is required to deploy: pass -c imageTag=<tag> (scripts/build_image.sh prints it). ' +
          '`cdk destroy DeciderStack` needs no tag.',
      );
      imageTag = PLACEHOLDER_IMAGE_TAG;
    }
    const dtype = this.node.tryGetContext('deciderDtype') ?? DEFAULT_DTYPE;
    const inferenceAmiVersion = this.node.tryGetContext('inferenceAmiVersion') ?? DEFAULT_INFERENCE_AMI;

    // Imported by name, so there is no cross-stack reference (and no CloudFormation export).
    const repository = ecr.Repository.fromRepositoryName(this, 'Repository', REPOSITORY_NAME);

    const role = new iam.Role(this, 'ExecutionRole', {
      // Confused-deputy protection: SageMaker may assume this role only for resources of this account.
      assumedBy: new iam.ServicePrincipal('sagemaker.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': this.formatArn({ service: 'sagemaker', resource: '*' }) },
        },
      }),
      description: 'Execution role of the CTF decider endpoint: pull its image, write its logs and metrics',
    });
    repository.grantPull(role);
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          this.formatArn({
            service: 'logs',
            resource: 'log-group',
            resourceName: `/aws/sagemaker/Endpoints/${ENDPOINT_NAME}*`,
            arnFormat: ArnFormat.COLON_RESOURCE_NAME,
          }),
        ],
      }),
    );

    // PutMetricData has no resource-level permissions, so the namespace condition is the only scoping.
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': ENDPOINT_METRICS_NAMESPACE } },
      }),
    );

    const model = new sagemaker.CfnModel(this, 'Model', {
      executionRoleArn: role.roleArn,
      // The weights are baked into the image and HF_HUB_OFFLINE=1: the container needs no network.
      enableNetworkIsolation: true,
      primaryContainer: {
        image: repository.repositoryUriForTag(imageTag),
        environment: { HF_HUB_OFFLINE: '1', DECIDER_DTYPE: dtype },
      },
    });

    // The pull permissions live in the role's default policy: it must exist before SageMaker pulls the image.
    model.node.addDependency(role);

    const config = new sagemaker.CfnEndpointConfig(this, 'EndpointConfig', {
      productionVariants: [
        {
          variantName: 'AllTraffic',
          modelName: model.attrModelName,
          instanceType: INSTANCE_TYPE,
          initialInstanceCount: 1,
          initialVariantWeight: 1,
          containerStartupHealthCheckTimeoutInSeconds: STARTUP_HEALTH_CHECK_SECONDS,
          inferenceAmiVersion,
        },
      ],
    });

    new sagemaker.CfnEndpoint(this, 'Endpoint', {
      endpointName: ENDPOINT_NAME,
      endpointConfigName: config.attrEndpointConfigName,
    });
  }
}
