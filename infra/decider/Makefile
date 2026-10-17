# Local, non-mutating targets first; the deploy/destroy/build-image targets mutate AWS.
# Deploy targets run with --require-approval never so they work in non-interactive shells (the CDK IAM
# prompt would otherwise hang). Review IAM changes first with the matching diff-* target.
CDK := node_modules/.bin/cdk

.PHONY: install build test test-adapter synth smoke-local build-image \
	diff-persistent diff-decider deploy-persistent deploy-decider destroy-decider

install:
	npm ci

build:
	npm run build

test:
	node_modules/.bin/jest

test-adapter:
	cd ../.. && uv run pytest tests/test_adapter_app.py tests/test_adapter_supervisor.py \
		tests/test_adapter_support.py tests/test_adapter_e2e.py tests/test_adapter_build.py

synth: build
	$(CDK) synth CtfPersistentStack -c budgetEmail=$${CTF_BUDGET_EMAIL:?set CTF_BUDGET_EMAIL} --quiet
	$(CDK) synth DeciderStack -c imageTag=$${IMAGE_TAG:-local-synth} --quiet

smoke-local:
	scripts/smoke_local.sh

# Starts a CodeBuild run and pushes to ECR. Usage: make build-image YES=1
build-image:
	scripts/build_image.sh $(if $(YES),--yes,)

# MUTATING. Once. Reads the budget email from the environment (never committed, never on the command line).
#   CTF_BUDGET_EMAIL=<address> make deploy-persistent
deploy-persistent: build
	$(if $(CTF_BUDGET_EMAIL),,$(error set CTF_BUDGET_EMAIL to the budget alert recipient))
	$(CDK) deploy CtfPersistentStack --require-approval never

# Read-only: show what a deploy would change (review IAM statements before deploying).
diff-persistent: build
	$(if $(CTF_BUDGET_EMAIL),,$(error set CTF_BUDGET_EMAIL to the budget alert recipient))
	$(CDK) diff CtfPersistentStack

diff-decider: build
	$(if $(IMAGE_TAG),,$(error set IMAGE_TAG to the tag printed by build-image))
	$(CDK) diff DeciderStack -c imageTag=$(IMAGE_TAG)

# MUTATING and BILLING: creates the GPU endpoint (about $0.74/h). The tag is what `make build-image`
# printed. Get the user's OK first and review `make diff-decider`.
#   IMAGE_TAG=<hash> make deploy-decider
# Without -c imageTag the stack carries an error annotation, so `cdk deploy` refuses to run.
deploy-decider: build
	$(if $(IMAGE_TAG),,$(error set IMAGE_TAG to the tag printed by build-image))
	$(CDK) deploy DeciderStack -c imageTag=$(IMAGE_TAG) --require-approval never

# MUTATING: stops the endpoint billing. Needs no tag and no email: `cdk destroy` does not validate the
# stack's error annotations. ECR, the Budget and the /ctf-decider/endpoint-name parameter
# (CtfPersistentStack) stay, so the decider runtimes keep starting and fall back to the LLM.
destroy-decider: build
	$(CDK) destroy DeciderStack --force
