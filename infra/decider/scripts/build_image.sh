#!/usr/bin/env bash
# Build the linux/amd64 GPU decider image in CodeBuild and push it to ECR.
#
# MUTATING: starts a CodeBuild run (costs money) and pushes to ECR. It refuses to do anything without --yes.
# The image tag is a content hash of the adapter source plus the model id, so an unchanged adapter and
# model never rebuild: if the tag is already in ECR the script prints it and exits.
#
#   scripts/build_image.sh                      # print the plan, change nothing
#   scripts/build_image.sh --yes                # build and wait
#
# The model is pinned in lib/names.ts (DEFAULT_MODEL_ID) and cannot be overridden here.
#
# Prerequisites: CtfPersistentStack is deployed (it uploads the adapter source and owns the project),
# and the adapter source has not changed since that deploy (the build verifies the hash and fails if so).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="$HERE/../adapter"
# The model id is pinned in ONE place, lib/names.ts (DEFAULT_MODEL_ID); there is no command-line override,
# so the tag and the image can only ever be about that model.
MODEL_ID="$(sed -n "s/^export const DEFAULT_MODEL_ID = '\(.*\)';\$/\1/p" "$HERE/../lib/names.ts")"
[[ -n "$MODEL_ID" ]] || { echo "cannot read DEFAULT_MODEL_ID from lib/names.ts" >&2; exit 1; }
REPOSITORY_NAME="ctf-decider"
POLL_SECONDS="${POLL_SECONDS:-20}"
MAX_POLLS="${MAX_POLLS:-180}" # 180 x 20 s = 60 min
YES=0

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

TAG="$(python3 "$ADAPTER_DIR/hash_source.py" --model-id "$MODEL_ID")"

echo "model id:    $MODEL_ID"
echo "image tag:   $TAG"
echo "repository:  $REPOSITORY_NAME (from SSM /ctf-decider/repo-uri)"

if [[ "$YES" -ne 1 ]]; then
  echo
  echo "This would start a CodeBuild run and push the image to ECR (costs money)." >&2
  echo "Nothing was done. Re-run with --yes to proceed." >&2
  exit 2
fi

project="$(aws ssm get-parameter --name /ctf-decider/build-project --query Parameter.Value --output text)"

# Only "the image does not exist" means "build it". Any other failure (credentials, permissions, throttling,
# a missing repository) must stop here instead of starting a paid build on a wrong assumption.
if describe_err="$(aws ecr describe-images --repository-name "$REPOSITORY_NAME" --image-ids "imageTag=$TAG" 2>&1 >/dev/null)"; then
  echo "image $TAG already exists in ECR, nothing to build"
  echo "imageTag=$TAG"
  exit 0
elif [[ "$describe_err" != *ImageNotFoundException* ]]; then
  echo "cannot tell whether image $TAG exists in ECR:" >&2
  echo "$describe_err" >&2
  exit 1
fi

build_id="$(aws codebuild start-build \
  --project-name "$project" \
  --environment-variables-override \
  "name=IMAGE_TAG,value=$TAG,type=PLAINTEXT" \
  "name=MODEL_ID,value=$MODEL_ID,type=PLAINTEXT" \
  --query build.id --output text)"
echo "started build $build_id"

for ((i = 0; i < MAX_POLLS; i++)); do
  status="$(aws codebuild batch-get-builds --ids "$build_id" --query 'builds[0].buildStatus' --output text)"
  case "$status" in
    SUCCEEDED)
      echo "build succeeded"
      echo "imageTag=$TAG"
      echo "next: cdk deploy DeciderStack -c imageTag=$TAG"
      exit 0
      ;;
    IN_PROGRESS) sleep "$POLL_SECONDS" ;;
    *)
      echo "build ended with status $status (see the CodeBuild logs for $build_id)" >&2
      exit 1
      ;;
  esac
done
echo "gave up waiting for $build_id after $((MAX_POLLS * POLL_SECONDS)) s" >&2
exit 1
