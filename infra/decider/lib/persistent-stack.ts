import { Annotations, Duration, RemovalPolicy, Stack, type StackProps } from 'aws-cdk-lib';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as codebuild from 'aws-cdk-lib/aws-codebuild';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import type { Construct } from 'constructs';
import { resolveAdapterDir } from './paths';
import { DEFAULT_MODEL_ID, REPOSITORY_NAME, SSM_BUILD_PROJECT, SSM_REPO_URI } from './names';

export interface CtfPersistentStackProps extends StackProps {
  /** Budget alert recipient. Falls back to context `budgetEmail`, then env CTF_BUDGET_EMAIL. */
  readonly budgetEmail?: string;
  /** Hugging Face model id baked into the image. Falls back to context `modelId`. */
  readonly modelId?: string;
  /** Directory of the SageMaker adapter (Dockerfile + adapter package). */
  readonly adapterDir?: string;
}

/** What a stack built without a budget email carries: it cannot be deployed (error annotation). */
export const PLACEHOLDER_EMAIL = 'placeholder@example.invalid';

export const MONTHLY_BUDGET_USD = 25;
export const KEEP_IMAGES = 3;

/**
 * Deployed once; it survives the weekly teardown of DeciderStack.
 * Holds the image repository, the image builder and the cost alert.
 */
export class CtfPersistentStack extends Stack {
  readonly repository: ecr.Repository;
  readonly buildProject: codebuild.Project;

  constructor(scope: Construct, id: string, props: CtfPersistentStackProps = {}) {
    super(scope, id, props);

    let email: string | undefined =
      props.budgetEmail || this.node.tryGetContext('budgetEmail') || process.env.CTF_BUDGET_EMAIL || undefined;
    if (!email) {
      // Still synthesizable (so it can be inspected or destroyed with no inputs), but never deployable.
      Annotations.of(this).addError(
        'A budget email is required to deploy: pass -c budgetEmail=<address> or set CTF_BUDGET_EMAIL.',
      );
      email = PLACEHOLDER_EMAIL;
    }
    const modelId = props.modelId ?? this.node.tryGetContext('modelId') ?? DEFAULT_MODEL_ID;
    const adapterDir = props.adapterDir ?? resolveAdapterDir(__dirname);

    this.repository = new ecr.Repository(this, 'Repository', {
      repositoryName: REPOSITORY_NAME,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      imageScanOnPush: true,
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [{ maxImageCount: KEEP_IMAGES, description: `Keep the last ${KEEP_IMAGES} images` }],
    });

    const source = new s3assets.Asset(this, 'AdapterSource', {
      path: adapterDir,
      exclude: ['**/__pycache__', '**/*.pyc', '**/.pytest_cache'],
    });

    this.buildProject = new codebuild.Project(this, 'ImageBuild', {
      description: 'Builds the linux/amd64 GPU decider image and pushes it to ECR',
      source: codebuild.Source.s3({ bucket: source.bucket, path: source.s3ObjectKey }),
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.LARGE,
        privileged: true,
      },
      environmentVariables: {
        MODEL_ID: { value: modelId },
        REPOSITORY_NAME: { value: REPOSITORY_NAME },
        REPOSITORY_URI: { value: this.repository.repositoryUri },
      },
      timeout: Duration.minutes(60),
      buildSpec: codebuild.BuildSpec.fromObject({
        version: '0.2',
        phases: {
          pre_build: {
            commands: [
              'echo "Tag $IMAGE_TAG for model $MODEL_ID"',
              'test -n "$IMAGE_TAG" || { echo "IMAGE_TAG is required (use scripts/build_image.sh)"; exit 1; }',
              // The tag is a content hash. A stale S3 source (adapter edited, stack not redeployed) fails here.
              'COMPUTED=$(python3 hash_source.py --model-id "$MODEL_ID")',
              'test "$COMPUTED" = "$IMAGE_TAG" || { echo "source hash $COMPUTED != $IMAGE_TAG: redeploy CtfPersistentStack"; exit 1; }',
              'aws ecr get-login-password | docker login --username AWS --password-stdin "${REPOSITORY_URI%%/*}"',
              // The base image (adapter/Dockerfile) is on ECR Public: anonymous pull, no other registry login.
            ],
          },
          build: {
            commands: [
              'docker build --platform linux/amd64 --build-arg MODEL_ID="$MODEL_ID" -t "$REPOSITORY_URI:$IMAGE_TAG" .',
            ],
          },
          post_build: {
            // post_build also runs after a failed build: never push an image that did not build.
            commands: [
              '[ "$CODEBUILD_BUILD_SUCCEEDING" = 1 ] || { echo "build failed: not pushing"; exit 1; }',
              'docker push "$REPOSITORY_URI:$IMAGE_TAG"',
            ],
          },
        },
      }),
    });
    // grantPullPush also grants ecr:GetAuthorizationToken, which the push to our own repository needs.
    this.repository.grantPullPush(this.buildProject);

    new budgets.CfnBudget(this, 'SageMakerBudget', {
      budget: {
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: MONTHLY_BUDGET_USD, unit: 'USD' },
        costFilters: { Service: ['Amazon SageMaker'] },
      },
      notificationsWithSubscribers: [
        { threshold: 80, notificationType: 'ACTUAL' },
        { threshold: 100, notificationType: 'FORECASTED' },
      ].map(({ threshold, notificationType }) => ({
        notification: {
          notificationType,
          comparisonOperator: 'GREATER_THAN',
          threshold,
          thresholdType: 'PERCENTAGE',
        },
        subscribers: [{ subscriptionType: 'EMAIL', address: email }],
      })),
    });

    new ssm.StringParameter(this, 'RepoUriParam', {
      parameterName: SSM_REPO_URI,
      stringValue: this.repository.repositoryUri,
    });
    new ssm.StringParameter(this, 'BuildProjectParam', {
      parameterName: SSM_BUILD_PROJECT,
      stringValue: this.buildProject.projectName,
    });
  }
}
