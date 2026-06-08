# Plan: local (laptop-only) transcription mode

**Goal:** a laptop-only path that takes a URL → downloads audio → transcribes →
writes `.txt` files locally, with **no AWS**. Reuse the existing download +
transcription logic via a shared core. **Hard constraint: do not change the
behavior of the working cloud pipeline.**

## Guiding principle: same code, two doors

There is no runtime "am I local or cloud?" detection. The environment is decided
entirely by **which entrypoint is invoked**:

- `python transcribe_local.py <url>` (you run it) → local path, never touches AWS.
- `lambda_handler.handler` (AWS Step Functions invokes it) → cloud path, never
  touches your laptop.

Both import the same environment-agnostic core. Differences (cookie source,
output destination) are passed *into* the core by each entrypoint, not detected.

## Current state

- `video_to_transcript.py` — `AudioDownloader` (download only). Already
  local-capable; already imported by the Lambda handler. **No change needed.**
- `lambda_handler.py` — contains the transcription + paragraph-formatting logic,
  but mixed in with S3/DynamoDB/chunking. The transcription functions themselves
  are already AWS-free.
- `requirements.txt` — yt-dlp, bgutil, boto3. `faster-whisper` is **not** here;
  it's pip-installed only inside the Dockerfile.

## Changes

### 1. New `transcribe_core.py` (extracted, AWS-free)
Move these **verbatim** out of `lambda_handler.py` (rename to public names):
- Paragraph tunables: `PAUSE_BREAK_S`, `MARKER_MIN_PARA_CHARS`, `MAX_PARA_CHARS`,
  `TOPIC_MARKERS`
- `get_model()` (was `_get_model`) — **one behavior tweak:** default model when
  `WHISPER_MODEL_PATH` is unset becomes `"base"` (faster-whisper auto-downloads
  it) instead of the hard-coded Lambda path. In Lambda the env var is *always*
  set (Dockerfile `ENV` + CloudFormation), so cloud behavior is unchanged;
  locally it falls back to auto-download.
- `whisper_segments()`, `ts()`, `format_timestamped()`, `format_paragraphs()`

### 2. Edit `lambda_handler.py` (minimal, pure relocation)
- Delete the moved definitions; add
  `from transcribe_core import whisper_segments, format_timestamped, format_paragraphs`
- Update the ~4 internal call sites (`_whisper_segments` → `whisper_segments`,
  etc.). Everything else (S3, DynamoDB, chunking, all `_step_*`) stays identical.

### 3. New `transcribe_local.py` (the local entrypoint)
- CLI: `python transcribe_local.py [-o OUTDIR] [--model base] [--keep-audio/--no-keep-audio] URL [URL ...]`
- Per URL: `AudioDownloader` (browser cookies, no S3) → mp3 → `whisper_segments`
  → write `<name>.txt` (timestamped) + `<name>-clean.txt` (paragraphs) to OUTDIR.
- **No chunking** — a laptop has no 15-min limit, so transcribe the whole file.
  (Chunking exists only to beat the Lambda deadline.)
- Imports `boto3` never get exercised locally.

### 4. Requirements split (cloud file untouched)
- `requirements.txt` — **leave exactly as-is** (cloud/Docker depends on it → zero
  cloud risk).
- New `requirements-local.txt` — `yt-dlp[default]`, `bgutil-ytdlp-pot-provider`,
  `faster-whisper`. (boto3 not needed locally.)

### 5. `Dockerfile` (one required, verified change)
- `COPY` line must add `transcribe_core.py` (the handler now imports it):
  `COPY video_to_transcript.py transcribe_core.py lambda_handler.py entrypoint.sh ./`
- **This is the one change that could break cloud if missed** → covered by the
  redeploy + smoke test below.

### 6. Docs
- README: add a "Run it locally (no AWS)" section — prereqs (ffmpeg, deno for
  YouTube, `pip install -r requirements-local.txt`), usage, where files land,
  note that first run downloads the ~150 MB whisper model.
- DEVLOG: record the core extraction + local mode.

## Local YouTube note
The local path uses browser cookies + yt-dlp's bundled Deno EJS solver. It does
**not** require running the bgutil POT server (that's a cloud concern). If
YouTube ever blocks, running a local POT server is a documented advanced option,
not a default requirement.

## Verification (the safety gate for "don't break cloud")
1. `python -m py_compile` all modules; `python transcribe_local.py` on
   "Me at the zoo" → confirm local `.txt` output is correct.
2. `./deploy.sh` (rebuilds image with the new `COPY`, redeploys) — must pass its
   built-in auth smoke test.
3. Submit "Me at the zoo" through the **live API** and confirm it reaches
   `done` with a correct transcript — proving the cloud path is intact after the
   refactor.
4. Only then: commit + push.

## Out of scope
- No change to API, Step Functions, DynamoDB, or the chunking path.
- Local CLI is single-process synchronous (no job queue) — that's the point.
</content>
