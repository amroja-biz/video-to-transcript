# Development Log - video-to-transcript

## About This Project

Submit a video URL (YouTube, Instagram, Facebook, X.com — anything yt-dlp
supports) and get back an MP3 plus a readable, paragraph-formatted transcript.
Two interfaces: a local Python CLI for quick grabs, and a token-protected HTTPS
endpoint on AWS that runs the whole download-and-transcribe pipeline
serverlessly and is callable from a phone. Built for one person who wants to
read videos instead of watching them.

**Status:** Active
**Started:** 2026-06-07
**Last Updated:** 2026-06-07

---

## 2026-06-07 - Project Inception: "a new version of youtube downloader"

The starting point was an old project in the scratch repo (`TOOLS/yt-downloader`)
— a Flask+React web app wrapping yt-dlp. The brief: rebuild it as a CLI-only,
audio-only tool that works across major sites, with a Dockerfile so it could run
on AWS. The CLI pattern was borrowed from a sibling project
(`TOOLS/instagram-downloader`): argparse, multi-URL positional args, a
downloader class using the yt_dlp Python library.

Notable 2026 reality check during planning: yt-dlp's YouTube extraction now
requires an external JavaScript runtime (Deno) for its challenge solver, and
`pip install "yt-dlp[default]"` bundles the solver scripts. The old project's
`--remote-components ejs:github` flag is no longer the way.

---

## 2026-06-07 - The Architecture Detour: GitHub Actions → back to AWS

Mid-planning, the plan pivoted toward borrowing the architecture of
`x-video-transcribe` (another scratch project) — offload work to GitHub Actions
so no infrastructure is needed. That idea died on a hard fact the
x-video-transcribe project itself documents: **YouTube blocks GitHub Actions
runner IPs**. Research confirmed the block isn't GitHub-specific — it's
datacenter-IP reputation, which means AWS has the same problem, only worse
(bot-walls after as few as 5–10 downloads from EC2 ranges).

The honest options were: run locally (residential IP), use authenticated
cookies on cloud IPs, or pay for a residential proxy. Decision: **AWS +
cookies file**, with one hard UX requirement — refreshing cookies must be one
command. That became `refresh-cookies.sh`: export the entire Chrome cookie jar
via yt-dlp (covers YouTube+IG+FB+X in one shot) and upload to S3. S3 won over
Secrets Manager because cookie jars routinely exceed Secrets Manager's 64KB cap
— validated immediately when the first real export came out at 99.6KB.

Architecture choice for the Docker image: **ARM64/Graviton**. ffmpeg, Deno, and
yt-dlp all run natively on arm64, Graviton is ~20% cheaper, and an Apple Silicon
Mac builds the image without emulation.

Phase 1 shipped and verified the same morning: CLI download working locally and
in the container, CloudFormation stack (bucket + ECR + IAM policy), image in
ECR, cookies in S3, and a full AWS-mode container run (S3 cookies in → MP3 →
S3 out). Two bugs found by actually running things: yt-dlp refuses to *write*
a `--cookies` file that already exists empty (mktemp pre-creates one), and the
bgutil PO-token server stalled >15s on first boot downloading npm deps (fixed
by pre-caching with `deno install` at image build).

---

## 2026-06-07 - The Endpoint: Lambda + DynamoDB + a token

Next request: trigger downloads from a protected endpoint — desktop and phone —
with request state in DynamoDB, protected by something as simple as a PIN. The
`aws-quick-endpoint` skill served as a reference (explicitly *not* a
dependency): its x-api-token header auth, NoEcho token parameter, HMAC-signed
pagination, and generated-USAGE.md patterns were re-implemented inside this
project, and the whole deploy flow was wrapped in a self-contained `deploy.sh`.

On auth strength: a 4-digit PIN behind 5 req/s throttling is brute-forceable in
~30 minutes, so the decision landed on a random UUID token — env var on
desktop, 1Password + an iOS Shortcut on the phone, so it never gets typed.

Then the requirement that reshaped everything: *the point of downloading audio
is reading transcripts*. The bucket became `amroja-audio` with `audio/` and
`transcripts/` folders, and the worker gained automatic transcription using
faster-whisper (same engine and settings as x-video-transcribe: base model, CPU,
int8, beam_size=5). Transcripts get paragraph breaks inserted at natural
boundaries — long pauses between whisper segments (≥1.5s), topic-shift
discourse markers ("So...", "And then..."), and runaway-paragraph caps — words
never altered, only breaks inserted.

---

## 2026-06-07 - Timeout Reality → Step Functions

A timeout audit changed the architecture once more. API Gateway's 30s cap was
never a risk, but a single Lambda caps at 15 minutes, and faster-whisper on
Lambda's ~2–3 vCPUs runs maybe 4–6× realtime — meaning a one-hour podcast can't
transcribe in one invocation. The fix: a **Step Functions state machine** that
chunks long audio (>20 min) into 10-minute segments with ffmpeg and transcribes
them in up to 8 parallel Lambda invocations, then merges segments with
continuous timestamps.

The deadline requirement ("give it a 15 minute timeout; if it fails, update the
job status in DynamoDB") collided with an ASL quirk: a top-level state machine
timeout aborts *uncatchably*, and — discovered at deploy time — `TimeoutSeconds`
isn't even legal on Parallel states. Final design: top-level machine timeout
(hard 900s kill) + catchable per-Task timeouts feeding a Catch →
DynamoDB-UpdateItem RecordError state + a read-time staleness fallback in the
API (any non-terminal job older than 20 minutes reports as timed-out). A job
can never appear stuck.

One container image serves everything: default ENTRYPOINT for docker-run CLI
mode, and Lambda mode selected purely via CloudFormation `ImageConfig`
(awslambdaric + `lambda_handler.handler`, WorkingDirectory /app). The worker
handler dispatches on a `step` field — download / transcribe / chunk /
transcribe_chunk / merge — so the whole state machine needs exactly one Lambda
function. The whisper base model is baked into the image at `/opt/whisper-models`
so the read-only Lambda filesystem never needs a runtime download.

The nastiest landmine was Deno on Lambda: the runtime user isn't root, the
filesystem is read-only, and Deno panics if its cache dir isn't writable *even
when fully pre-cached* (deno issues #25596, #26747). Solution: bake the cache
to `/opt/deno-cache` world-readable, copy it to `/tmp/deno-cache` at cold
start, run with `DENO_DIR=/tmp/deno-cache HOME=/tmp`.

Deploy-time bugs, in order of discovery: buildx's default OCI attestation
manifests are rejected by Lambda (`--provenance=false --sbom=false`); the
Parallel-state timeout schema error; and a zsh footgun where `$ECR:latest`
silently invoked the `:l` lowercase modifier and mangled the image tag.

### Verified end-to-end
POST "Me at the zoo" → 202 → `queued → downloading → transcribing → done` in
~70 seconds including cold start. The transcript came back accurate and the
presigned MP3 played. Critically, **the YouTube download succeeded from a
Lambda datacenter IP** — migrated browser cookies + the in-Lambda PO-token
server doing their job. Error path verified too (bad URL → `status=error` with
the cause). The chunked >20-min path is deployed but hasn't had a live run yet.

### Chunked path verified (2026-06-07)
A 42.6-minute YouTube video (2,555s) exercised the long-audio branch live:
Choice → ChunkAudio → Map with **5 parallel transcribe_chunk iterations**
(4×600s + 155s) → merge. 8 Lambda invocations total, **4m17s wall time**
end-to-end. Verified in the merged transcript: timestamps are monotone across
all four 10-minute boundaries (offsets applied correctly), content runs to
42:34, and `chunks/<id>/` intermediates were cleaned up from S3. Known minor
artifact: a word or two can repeat/clip at chunk seams because ffmpeg segments
with `-c copy` (no overlap) — acceptable for now; overlapping chunks would fix
it if it ever matters. Same day, X.com and Instagram URLs were also verified
end-to-end through the short path.

---

## 2026-06-07 - Graduated Out of Scratch

The project outgrew the scratch repo and moved to its own repository at
`~/code/video-to-transcript` (fresh git repo, no commits yet at time of
writing). Everything is path-relative so nothing broke; the AWS stack
(`audio-downloader`, profile `sandbox`, us-east-1) and the live endpoint were
untouched by the move.

Operational notes for future-me:
- `./deploy.sh` — build, push, deploy, smoke-test; prints endpoint + token
  on success (usage examples live in README.md)
- `./refresh-cookies.sh` — run when cloud downloads start hitting bot-walls
- `.api-token` holds the endpoint token (gitignored; CFN NoEcho means it can't
  be recovered from the stack — don't lose it)
- yt-dlp churn is the standing maintenance burden: rebuild the image to pick up
  updates, keep `BGUTIL_TAG` in the Dockerfile in sync with the pip plugin

---
