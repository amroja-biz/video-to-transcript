#!/usr/bin/env bash
# One-command setup/deploy for video-to-transcript: builds and pushes the worker
# image, manages the API auth token, deploys CloudFormation, points the worker
# Lambda at the new image, smoke-tests the endpoint, and prints the API
# endpoint + token on success.
#
# Usage: ./deploy.sh [-p aws-profile] [-r region]
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${AWS_PROFILE:-sandbox}"
REGION="us-east-1"
STACK="video-to-transcript"

while getopts "p:r:" opt; do
  case $opt in
    p) PROFILE="$OPTARG" ;;
    r) REGION="$OPTARG" ;;
    *) echo "Usage: $0 [-p profile] [-r region]" >&2; exit 2 ;;
  esac
done

AWS=(aws --profile "$PROFILE" --region "$REGION")
ACCOUNT=$("${AWS[@]}" sts get-caller-identity --query Account --output text)
ECR_HOST="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
ECR_URI="${ECR_HOST}/video-to-transcript"

stack_output() {
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# --- 1. Build & push the image FIRST (Lambda needs lambda_handler.py inside) --
echo "==> Building image (arm64) ..."
# --provenance/--sbom=false: Lambda rejects OCI manifest lists with
# attestation manifests; it needs a plain single-arch image manifest.
docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
  -t "${ECR_URI}:latest" --load . >/dev/null

echo "==> Pushing to ECR ..."
# The ECR repo is created by the stack; on the very first run it may not exist
# yet, so create it on the fly (CloudFormation will adopt it by name? No — it
# can't adopt; instead, push only if the repo exists, else defer the push).
if "${AWS[@]}" ecr describe-repositories --repository-names video-to-transcript >/dev/null 2>&1; then
  "${AWS[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$ECR_HOST" >/dev/null
  docker push "${ECR_URI}:latest" >/dev/null
  PUSHED=1
else
  echo "    ECR repo not found (first run) — will push after the stack creates it."
  PUSHED=0
fi

# --- 2. Auth token: persist locally (NoEcho params can't be recovered) -------
if [[ ! -f .api-token ]]; then
  python3 -c "import uuid; print(uuid.uuid4())" > .api-token
  chmod 600 .api-token
  echo "==> Generated new API token (.api-token)"
fi
TOKEN="$(cat .api-token)"

# --- 3. Deploy the stack ------------------------------------------------------
# First run (no ECR repo yet): deploy storage-only (ProvisionCompute=false) so
# the stack creates the repo, push the image, then deploy compute too.
echo "==> Deploying CloudFormation stack ..."
if [[ "$PUSHED" == "0" ]]; then
  echo "    First run: deploying storage-only stack to create the ECR repo ..."
  "${AWS[@]}" cloudformation deploy \
    --template-file cloudformation.yaml \
    --stack-name "$STACK" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "ApiAuthToken=${TOKEN}" "ProvisionCompute=false" \
    --no-fail-on-empty-changeset
  echo "==> Pushing image to newly created repo ..."
  "${AWS[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$ECR_HOST" >/dev/null
  docker push "${ECR_URI}:latest" >/dev/null
fi
"${AWS[@]}" cloudformation deploy \
  --template-file cloudformation.yaml \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "ApiAuthToken=${TOKEN}" "ProvisionCompute=true" \
  --no-fail-on-empty-changeset

# --- 4. Point the worker Lambda at the freshly pushed digest ------------------
WORKER=$(stack_output WorkerFunctionName)
if [[ -n "$WORKER" && "$WORKER" != "None" ]]; then
  echo "==> Updating worker Lambda image ..."
  "${AWS[@]}" lambda update-function-code --function-name "$WORKER" \
    --image-uri "${ECR_URI}:latest" >/dev/null
  "${AWS[@]}" lambda wait function-updated --function-name "$WORKER"
fi

# --- 5. Smoke test -------------------------------------------------------------
API=$(stack_output ApiUrl)
echo "==> Smoke testing $API ..."
GOOD=$(curl -s -o /dev/null -w '%{http_code}' "$API/downloads?limit=1" -H "x-api-token: $TOKEN")
BAD=$(curl -s -o /dev/null -w '%{http_code}' "$API/downloads?limit=1" -H "x-api-token: wrong")
if [[ "$GOOD" == "200" && "$BAD" == "401" ]]; then
  echo "    ✓ auth OK (200 with token, 401 without)"
else
  echo "    ✗ smoke test unexpected: with-token=$GOOD (want 200), bad-token=$BAD (want 401)" >&2
  exit 1
fi

# --- 6. Print endpoint + token -------------------------------------------------
echo
echo "✓ Deployed."
echo
echo "  API endpoint: $API"
echo "  API token:    $TOKEN"
echo
echo "  SAVE THE TOKEN somewhere safe (e.g. 1Password). It is also persisted"
echo "  locally in .api-token (gitignored). Usage examples: README.md"
