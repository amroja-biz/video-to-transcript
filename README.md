# video-to-transcript

Turn a video/audio URL into a **transcript**. Give it a link from YouTube,
Instagram, Facebook, X.com — anything
[yt-dlp](https://github.com/yt-dlp/yt-dlp) supports — and it downloads the
audio, transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
and produces paragraph-formatted text. Two ways to run it, same transcription
core:

- **AWS endpoint** — a private HTTPS API; submit from `curl` or an iPhone
  Shortcut, results land in S3. The main, always-on path.
- **Locally on your laptop** — one command, no AWS, transcripts written to a
  folder. See [Run it locally (no AWS)](#run-it-locally-no-aws).

## How it works

Everything runs **serverless on AWS — no servers to manage**:

```
POST /downloads ──▶ API Gateway ──▶ API Lambda
                                       │  writes job to DynamoDB (status=queued)
                                       └─ starts a Step Functions execution
                                              │
                    ┌─────────────────────────┴─────────────────────────┐
                    │  Worker Lambda (one container image, many steps)   │
                    │  download (yt-dlp) ─▶ short?  transcribe whole file │
                    │                       long?   chunk ─▶ N parallel   │
                    │                                transcribe ─▶ merge   │
                    └─────────────────────────┬─────────────────────────┘
                                              ▼
              MP3 + transcripts in S3,  status/result in DynamoDB
```

- **API Gateway + API Lambda** — auth, validation, job creation, reads.
- **Step Functions** — orchestrates the pipeline with a hard 15-minute deadline.
  Audio over 20 minutes is split into chunks and transcribed in parallel
  (up to 8 at once), then merged.
- **Worker Lambda** — a single container image (arm64) that handles every
  compute step. It runs on **Lambda's Graviton (arm64)** runtime, which is why
  the image is built for arm64.
- **S3** — stores cookies (input), MP3s, and transcripts. **DynamoDB** — job
  state. MP3s auto-expire after 30 days; transcripts are kept.

**Why cookies:** YouTube/Instagram/Facebook block datacenter IPs (including
AWS). The pipeline authenticates with a cookies file you export from your own
browser and store in S3 — one command, see [Refresh cookies](#refresh-cookies).

## Prerequisites

Deploying and refreshing cookies happen from your Mac. You need:

- **Docker** (Apple Silicon builds the arm64 image natively) and the **AWS CLI**
  configured with the `sandbox` profile.
- A **local Python venv** — used by `refresh-cookies.sh` to export your browser
  cookies:

  ```bash
  brew install python@3.14
  /opt/homebrew/opt/python@3.14/bin/python3.14 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```

## Deploy

One command builds the image, deploys all the infrastructure, and prints your
endpoint + token:

```bash
./deploy.sh            # AWS profile "sandbox", us-east-1 by default
```

On success it prints the **API endpoint and a secret token** — save the token
(e.g. in 1Password). It's also written to the gitignored `.api-token` locally
and can't be recovered from AWS afterward. Re-run `./deploy.sh` after any code
change. First-run deploys take a few minutes (image build + two-phase stack
creation).

## Use it from the command line

Set these to the values `deploy.sh` printed:

```bash
URL="https://<api-id>.execute-api.us-east-1.amazonaws.com"
TOKEN="<your-api-token>"
```

**1. Submit a URL** (returns a job id immediately; transcription runs in the background):

```bash
curl -s -X POST "$URL/downloads" \
  -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}'
# -> {"id": "dd752f77-...", "status": "queued"}
```

**2. Check status / read the transcript** (poll until `status` is `done`):

```bash
curl -s "$URL/downloads/<job-id>" -H "x-api-token: $TOKEN"
```

Status flow: `queued → downloading → downloaded → transcribing → done | error`.
A short clip is `done` in well under a minute; a 40-minute video takes a few
minutes. When `done`, the response includes:

- `transcript` — the paragraph-formatted transcript, inline (up to 50KB)
- `transcript_url` / `transcript_clean_url` — presigned links to the full
  timestamped and clean transcript files
- `download_url` — presigned MP3 link (valid 1h; GET the job again for a fresh one)

**List recent jobs:**

```bash
curl -s "$URL/downloads?limit=25" -H "x-api-token: $TOKEN"
# pass the returned next_token as ?next_token=... to page
```

### Tested examples

```bash
# YouTube
curl -s -X POST "$URL/downloads" -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://www.youtube.com/watch?v=ACRd0Ikg_KI&t=1632s"}'

# X
curl -s -X POST "$URL/downloads" -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://x.com/jasminewsun/status/2061871693891776808/video/1"}'

# Instagram
curl -s -X POST "$URL/downloads" -H "Content-Type: application/json" \
  -H "x-api-token: $TOKEN" \
  -d '{"url": "https://www.instagram.com/reel/DZSmUechRoe/?igsh=MW14MmoycXMwemNhcg=="}'
```

## Use it from your iPhone

Build a Shortcut once, then transcribe any video by tapping **Share → your
Shortcut** in YouTube, Safari, X, Instagram, etc. The Shortcut submits the
shared URL and shows you the transcript when it's ready.

> **Where's the Shortcuts app?** It's a **built-in Apple app** — you don't need
> to download anything (if it was deleted, it's free in the App Store under
> "Shortcuts" by Apple). Its icon is **two overlapping rounded squares on a
> blue-to-pink gradient**. Don't confuse it with **Settings → Shortcuts**, which
> is just a configuration page, not the app. Easiest way to open the real app:
> swipe down on the home screen, search **"Shortcuts"**, and tap the app (the
> gradient icon). Inside, you'll see a grid of shortcuts and a **+** to add one.

**Build the Shortcut:**

1. Open the **Shortcuts** app → tap **+** (top-right) to create a new shortcut →
   name it "Transcribe".
2. Tap the **ⓘ** (info) button → enable **Show in Share Sheet**, then close the
   panel.
   - **Restrict it to links (optional but tidy):** after enabling the toggle, a
     banner appears at the **top of the shortcut** reading **"Receive [Any]
     input from Share Sheet"**. Tap the highlighted **"Any"** to open the input-
     types checklist, then deselect everything except **URLs**. (Older iOS shows
     this as a "Share Sheet Types" row under the toggle instead — same setting.)
   - This step only keeps the shortcut from appearing in the Share sheet for
     non-link content. If you can't find it, leave it as **"Any"** — the
     shortcut still works fine when you share a video link.
3. Add these actions in order (tap **+ Add Action** for each):

   **a. Get Contents of URL** — this submits the job. Configure it:
   - URL: `https://<api-id>.execute-api.us-east-1.amazonaws.com/downloads`
   - Tap **Show More**:
     - Method: **POST**
     - Headers: add `x-api-token` = `<your-api-token>`, and
       `Content-Type` = `application/json`
     - Request Body: **JSON**, add a field `url` (type Text) and set its
       value to the **Shortcut Input** variable (tap the field → select the
       magic variable "Shortcut Input").

   **b. Get Dictionary Value** — pull the job id out of the response.
   Set Key to `id`, Dictionary to the output of step (a). (Save the result
   to a variable named `jobId` if you like.)

   **c. Repeat 30 Times** (a loop that polls until the transcript is ready).
   Inside the loop:
   - **Get Contents of URL** — URL
     `https://<api-id>.execute-api.us-east-1.amazonaws.com/downloads/` followed
     by the `id` variable from step (b). Method **GET**, add the same
     `x-api-token` header. (No body.)
   - **Get Dictionary Value** — Key `status` from that response → **If**
     `status` **is** `done`: add **Get Dictionary Value** Key `transcript`,
     then **Show Result** (or **Copy to Clipboard**), then **Stop Shortcut**.
   - **Wait** 10 seconds (so the loop polls roughly every 10s).

4. Done. Now in any app, **Share** a video → **Transcribe**. After a short
   wait the transcript pops up.

> Tip: if you'd rather keep it dead simple, you can skip the polling loop
> (steps c) — just submit in step (a), **Show Result** of the job id, and look
> the transcript up later with `curl` or by re-running a second "check status"
> Shortcut. The loop above is the hands-free version.

Keep the token private — anyone with the endpoint URL and token can submit
jobs. Store it in 1Password as a backup.

## Run it locally (no AWS)

Prefer to keep everything on your laptop? `transcribe_local.py` does the whole
job — download + transcribe — in one process, writing transcripts to a folder.
No AWS, no cookies-in-S3, no job queue. It shares the exact same download and
transcription code as the cloud path.

**Setup** (one time):

```bash
brew install ffmpeg deno          # ffmpeg always; deno only for YouTube
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local.txt
```

**Use it:**

```bash
source .venv/bin/activate

# One or more URLs → transcripts/YYYY-MM-DD/<title>.txt (+ -clean.txt)
python transcribe_local.py "https://www.youtube.com/watch?v=jNQXAC9IVRw"

# Custom output dir, bigger model, drop the MP3 afterward
python transcribe_local.py -o ~/transcripts --model small --no-keep-audio URL
```

Each URL produces two files next to the audio: `<title>.txt` (timestamped) and
`<title>-clean.txt` (paragraph-formatted). Notes:

- Uses your **Chrome cookies automatically** (first run prompts for macOS
  Keychain access — click "Always Allow"). Override with `--cookies-from-browser
  firefox` or `--cookies cookies.txt`.
- The **first run downloads the whisper model** (~150 MB for the default
  `base`); after that it's cached. `--model` accepts `tiny`/`base`/`small`/
  `medium`/`large-v3` or a local path — bigger is more accurate but slower.
- **No chunking** here — unlike the cloud path (which splits long audio to beat
  Lambda's 15-minute limit), the laptop just transcribes the whole file.

## Refresh cookies

Run this whenever cloud downloads start failing with bot/auth errors (e.g.
"Sign in to confirm you're not a bot"). It exports your browser's cookie jar
and uploads it to S3 where the pipeline reads it:

```bash
./refresh-cookies.sh                              # Chrome, bucket + profile from defaults
./refresh-cookies.sh -b my-bucket -p other-profile -B firefox
```

One export covers YouTube, Instagram, Facebook, and X. The S3 object is
versioned, so a bad export can be rolled back.

## Maintenance & ops

- **Redeploy after code changes:** `./deploy.sh` (rebuilds the image, updates
  the worker Lambda, re-passes the saved token).
- **yt-dlp drifts** as sites change. If extraction starts failing across the
  board, rebuild fresh to pick up the latest yt-dlp:
  `docker build --no-cache .` then `./deploy.sh`. Keep `BGUTIL_TAG` in the
  Dockerfile in sync with `bgutil-ytdlp-pot-provider` in `requirements.txt`.
- **Tear down:** empty the S3 bucket, then
  `aws cloudformation delete-stack --stack-name video-to-transcript --profile sandbox`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Job ends in `error` with a bot/auth message | Cookies expired → `./refresh-cookies.sh`, then resubmit |
| Downloads worked before, now all fail | yt-dlp drift → rebuild the image (`docker build --no-cache .`) and `./deploy.sh` |
| Job stuck and never reaches `done` | The pipeline has a hard 15-minute deadline; a job past that is reported as `error` on the next status check |
| "PO-token server did not start" in worker logs | YouTube may fail while other sites still work; check the worker Lambda logs in CloudWatch |
