#!/usr/bin/env bash
# One-command setup/deploy for audio-downloader: builds and pushes the worker
# image, manages the API auth token, deploys CloudFormation, points the worker
# Lambda at the new image, smoke-tests the endpoint, and writes USAGE.md.
#
# Usage: ./deploy.sh [-p aws-profile] [-r region]
set -euo pipefail
cd "$(dirname "$0")"

PROFILE="${AWS_PROFILE:-sandbox}"
REGION="us-east-1"
STACK="audio-downloader"

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
ECR_URI="${ECR_HOST}/audio-downloader"

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
if "${AWS[@]}" ecr describe-repositories --repository-names audio-downloader >/dev/null 2>&1; then
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
# First run: deploy infra-only first (worker Lambda needs the image in ECR).
echo "==> Deploying CloudFormation stack ..."
if [[ "$PUSHED" == "0" ]]; then
  echo "    First run: this will fail at WorkerFunction if the image isn't pushed."
  echo "    Strategy: create stack; if WorkerFunction fails, push image and retry."
fi
"${AWS[@]}" cloudformation deploy \
  --template-file cloudformation.yaml \
  --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "ApiAuthToken=${TOKEN}" \
  --no-fail-on-empty-changeset

if [[ "$PUSHED" == "0" ]]; then
  echo "==> Pushing image to newly created repo ..."
  "${AWS[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$ECR_HOST" >/dev/null
  docker push "${ECR_URI}:latest" >/dev/null
fi

# --- 4. Point the worker Lambda at the freshly pushed digest ------------------
WORKER=$(stack_output WorkerFunctionName)
if [[ -n "$WORKER" && "$WORKER" != "None" ]]; then
  echo "==> Updating worker Lambda image ..."
  "${AWS[@]}" lambda update-function-code --function-name "$WORKER" \
    --image-uri "${ECR_URI}:latest" >/dev/null
  "${AWS[@]}" lambda wait function-updated --function-name "$WORKER"
fi

# --- 5. Migrate cookies from the old bucket (one-time) ------------------------
BUCKET=$(stack_output BucketName)
OLD_BUCKET="audio-downloader-${ACCOUNT}"
if [[ "$BUCKET" != "$OLD_BUCKET" ]] \
   && "${AWS[@]}" s3api head-bucket --bucket "$OLD_BUCKET" >/dev/null 2>&1; then
  if "${AWS[@]}" s3api head-object --bucket "$OLD_BUCKET" --key cookies/cookies.txt >/dev/null 2>&1; then
    echo "==> Migrating cookies from $OLD_BUCKET to $BUCKET ..."
    "${AWS[@]}" s3 cp "s3://$OLD_BUCKET/cookies/cookies.txt" "s3://$BUCKET/cookies/cookies.txt" --sse AES256 >/dev/null
  fi
  echo "    NOTE: old bucket $OLD_BUCKET still exists. After verifying, remove it with:"
  echo "      aws s3 rb s3://$OLD_BUCKET --force --profile $PROFILE"
fi

# --- 6. Smoke test -------------------------------------------------------------
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

# --- 7. Write USAGE.md ---------------------------------------------------------
cat > USAGE.md <<EOF
# audio-downloader API

**Endpoint:** \`$API\`
**Auth token:** \`$TOKEN\`  *(keep private — this file is gitignored)*

## Setup

\`\`\`bash
TOKEN="$TOKEN"
URL="$API"
\`\`\`

## Submit a download + transcription job

\`\`\`bash
curl -s -X POST "\$URL/downloads" \\
  -H "Content-Type: application/json" \\
  -H "x-api-token: \$TOKEN" \\
  -d '{"url": "https://www.youtube.com/watch?v=..."}'
# -> {"id": "<job-id>", "status": "queued"}
\`\`\`

## Check status / read the transcript

\`\`\`bash
curl -s "\$URL/downloads/<job-id>" -H "x-api-token: \$TOKEN"
\`\`\`

Status flow: \`queued → downloading → downloaded → transcribing → done | error\`.
When \`done\`, the response includes:
- \`transcript\` — paragraph-formatted transcript text, inline (truncated at 50KB)
- \`download_url\` — presigned MP3 link (1h; if it 403s, GET again for a fresh one)
- \`transcript_url\` / \`transcript_clean_url\` — presigned transcript files

## List jobs

\`\`\`bash
curl -s "\$URL/downloads?limit=25" -H "x-api-token: \$TOKEN"
# pass next_token from the response to page
\`\`\`

## Phone usage (iOS Shortcut)

1. Shortcuts → + → "Receive **URLs** from Share Sheet"
2. Add action **Get Contents of URL**:
   - URL: \`$API/downloads\`  — Method: POST
   - Headers: \`x-api-token: $TOKEN\`, \`Content-Type: application/json\`
   - Request Body (JSON): \`url\` = Shortcut Input
3. Name it "Transcribe Audio". Now Share → Transcribe Audio from YouTube/IG/X.
   (Store the token in 1Password as backup.)

## Ops

- Refresh cookies when downloads fail with bot/auth errors: \`./refresh-cookies.sh\`
- Redeploy after code changes: \`./deploy.sh\`
- Tear down: \`aws cloudformation delete-stack --stack-name $STACK --profile $PROFILE\`
  (bucket must be emptied first)
EOF

echo
echo "✓ Deployed. Endpoint + token + examples: USAGE.md"
echo "  API: $API"
