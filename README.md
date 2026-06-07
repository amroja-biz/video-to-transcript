# video-to-transcript

Audio-only downloader **with automatic transcription**. Give it URLs from
YouTube, Instagram, Facebook, X.com — anything
[yt-dlp](https://github.com/yt-dlp/yt-dlp) supports — and it produces MP3s plus
paragraph-formatted transcripts (faster-whisper). Two ways to use it:

1. **Local CLI** — `python audio_downloader.py <url>` (download only)
2. **HTTPS endpoint on AWS** — `./deploy.sh` once, then POST a URL from desktop
   or phone; a Step Functions pipeline downloads, transcribes (chunking long
   audio in parallel, hard 15-min deadline), stores everything in
   `s3://amroja-audio/{audio,transcripts}/`, and tracks job state in DynamoDB.
   See [API usage](#api-usage) below for curl and iOS Shortcut examples.

**Why cookies matter:** YouTube/Instagram/Facebook block datacenter IPs (AWS,
GitHub Actions, etc.). Cloud runs therefore authenticate with a cookies file you
export from your own browser and park in S3. Refreshing it is one command:
`./refresh-cookies.sh`.

**Why ARM64/Graviton:** ffmpeg, Deno, and yt-dlp all run natively on arm64;
Graviton instances are ~20% cheaper than x86; and Apple Silicon Macs build the
arm64 image natively. Use Graviton instances (t4g/m7g/c7g) to run the container.

## Local setup

```bash
brew install python@3.14 deno   # deno only needed for YouTube
cd video-to-transcript
/opt/homebrew/opt/python@3.14/bin/python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Local usage

```bash
# Single URL (uses your Chrome cookies automatically)
python audio_downloader.py "https://www.youtube.com/watch?v=..."

# Multiple URLs, custom output dir
python audio_downloader.py -o ~/Music/rips "https://instagram.com/reel/..." "https://x.com/i/status/..."

# Explicit cookies file / different browser
python audio_downloader.py --cookies cookies.txt URL
python audio_downloader.py --cookies-from-browser firefox URL
```

MP3s land in `downloads/YYYY-MM-DD/`. First Chrome-cookie run prompts for
macOS Keychain access — click "Always Allow".

## AWS setup (one command)

```bash
./deploy.sh            # profile sandbox, us-east-1 by default
```

This builds + pushes the arm64 image, generates/persists the API token
(`.api-token`), deploys the CloudFormation stack (bucket `amroja-audio`,
DynamoDB job table, worker Lambda, Step Functions pipeline, throttled HTTP
API), and smoke-tests auth. On success it prints the **API endpoint and
token to the console — save the token** (e.g. in 1Password; it's also kept
locally in the gitignored `.api-token`). Re-run after any code change.

## API usage

Set these from the values `deploy.sh` printed:

```bash
URL="https://<api-id>.execute-api.us-east-1.amazonaws.com"
TOKEN="<your-api-token>"
```

### Submit a job, check status, get the transcript

```bash
# Submit (returns {"id": "<job-id>", "status": "queued"})
curl -s -X POST "$URL/downloads" \
  -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://www.youtube.com/watch?v=..."}'

# Status
curl -s "$URL/downloads/<job-id>" -H "x-api-token: $TOKEN"

# Clean transcript (presigned URL)
curl -s "$URL/downloads/<job-id>" -H "x-api-token: $TOKEN" | jq .transcript_clean_url
```

Status flow: `queued → downloading → downloaded → transcribing → done | error`.
When `done`, the response includes:
- `transcript` — paragraph-formatted transcript text, inline (truncated at 50KB)
- `download_url` — presigned MP3 link (1h; if it 403s, GET again for a fresh one)
- `transcript_url` / `transcript_clean_url` — presigned transcript files

### Tested examples (YouTube, X, Instagram)

```bash
# YouTube
curl -s -X POST "$URL/downloads" \
  -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://www.youtube.com/watch?v=ACRd0Ikg_KI&t=1632s"}'

# X
curl -s -X POST "$URL/downloads" \
  -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://x.com/jasminewsun/status/2061871693891776808/video/1"}'

# Instagram
curl -s -X POST "$URL/downloads" \
  -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://www.instagram.com/reel/DZSmUechRoe/?igsh=MW14MmoycXMwemNhcg=="}'
```

### List jobs

```bash
curl -s "$URL/downloads?limit=25" -H "x-api-token: $TOKEN"
# pass next_token from the response to page
```

### Phone usage (iOS Shortcut)

1. Shortcuts → + → "Receive **URLs** from Share Sheet"
2. Add action **Get Contents of URL**:
   - URL: `$URL/downloads`  — Method: POST
   - Headers: `x-api-token: <your-api-token>`, `Content-Type: application/json`
   - Request Body (JSON): `url` = Shortcut Input
3. Name it "Transcribe Audio". Now Share → Transcribe Audio from YouTube/IG/X.

### Ops

- Refresh cookies when downloads fail with bot/auth errors: `./refresh-cookies.sh`
- Redeploy after code changes: `./deploy.sh`
- Tear down: `aws cloudformation delete-stack --stack-name audio-downloader --profile sandbox`
  (bucket must be emptied first)

## Refresh cookies (do this whenever cloud downloads start failing with bot/auth errors)

```bash
./refresh-cookies.sh                 # uses $AUDIO_DL_BUCKET and profile "sandbox"
./refresh-cookies.sh -b my-bucket -p other-profile -B firefox
```

Exports your entire Chrome cookie jar (covers YouTube + IG + FB + X) and
uploads it to `s3://$AUDIO_DL_BUCKET/cookies/cookies.txt` (versioned, so you
can roll back).

## Build & push the image

```bash
docker build -t audio-downloader .            # arm64 on Apple Silicon
ECR=$(aws cloudformation describe-stacks --stack-name audio-downloader \
  --query 'Stacks[0].Outputs[?OutputKey==`EcrRepositoryUri`].OutputValue' \
  --output text --profile sandbox)
aws ecr get-login-password --profile sandbox | docker login --username AWS --password-stdin "${ECR%%/*}"
docker tag audio-downloader "$ECR:latest"
docker push "$ECR:latest"
```

Need x86? `docker buildx build --platform linux/amd64 -t audio-downloader .`

## Run on AWS (Graviton EC2 with the policy attached)

```bash
docker run --rm \
  -e AUDIO_DL_COOKIES_S3=s3://$AUDIO_DL_BUCKET/cookies/cookies.txt \
  -e AUDIO_DL_S3_OUTPUT=s3://$AUDIO_DL_BUCKET/audio/ \
  "$ECR:latest" "https://www.youtube.com/watch?v=..."

# Retrieve the MP3
aws s3 cp s3://$AUDIO_DL_BUCKET/audio/<file>.mp3 . --profile sandbox
```

ECS alternative: register a Fargate task definition (`runtimePlatform:
{cpuArchitecture: ARM64}`) with the same image, env vars, and the RunnerPolicy
on the task role, then `aws ecs run-task` per download.

MP3s under `audio/` auto-expire after 30 days (CloudFormation parameter).

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Sign in to confirm you're not a bot" / auth errors | Cookies expired → `./refresh-cookies.sh` |
| "no suitable JS runtime" (local YouTube) | `brew install deno` |
| "PO-token server did not start" in container logs | Check `/tmp/bgutil.log` in the container; YouTube may fail, other sites fine |
| Downloads work locally but not on AWS | That's the datacenter-IP wall — confirm cookies are fresh and the POT server started |
| yt-dlp extraction errors after months | Rebuild the image (`docker build --no-cache`) to pick up new yt-dlp; keep `BGUTIL_TAG` in sync with requirements.txt |
