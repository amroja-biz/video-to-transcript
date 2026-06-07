# audio-downloader

Audio-only downloader **with automatic transcription**. Give it URLs from
YouTube, Instagram, Facebook, X.com — anything
[yt-dlp](https://github.com/yt-dlp/yt-dlp) supports — and it produces MP3s plus
paragraph-formatted transcripts (faster-whisper). Two ways to use it:

1. **Local CLI** — `python audio_downloader.py <url>` (download only)
2. **HTTPS endpoint on AWS** — `./deploy.sh` once, then POST a URL from desktop
   or phone; a Step Functions pipeline downloads, transcribes (chunking long
   audio in parallel, hard 15-min deadline), stores everything in
   `s3://amroja-audio/{audio,transcripts}/`, and tracks job state in DynamoDB.
   See the generated **USAGE.md** for your endpoint, token, and curl/iOS
   Shortcut examples.

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
cd TOOLS/audio-downloader
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
API), smoke-tests auth, and writes **USAGE.md** with copy-paste examples.
Re-run it after any code change.

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
