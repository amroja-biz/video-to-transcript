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
**Last Updated:** 2026-06-08

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

### Full rename: audio-downloader → video-to-transcript (2026-06-07)
Everything renamed, including live infrastructure. CloudFormation can't rename
a stack, so this was create-new + migrate + delete-old:
- Code: `audio_downloader.py` → `video_to_transcript.py`, all `AUDIO_DL_*`
  env vars → `V2T_*` (Python, Dockerfile, CloudFormation, refresh-cookies.sh)
- New stack `video-to-transcript`: new bucket `amroja-video-to-transcript`,
  new ECR repo `video-to-transcript`, new API endpoint, **new everything-IDs
  but the same API token** (re-passed from `.api-token`)
- deploy.sh first-run flow fixed properly: it now uses the template's
  `ProvisionCompute=false` → push image → `ProvisionCompute=true` two-phase
  instead of the old "let WorkerFunction fail and retry" strategy. Verified
  live — the fresh stack came up in one `./deploy.sh` run.
- Data migrated: `aws s3 sync` of cookies + transcripts + MP3s (13 objects)
- Verified end-to-end on the new stack ("Me at the zoo" → done, transcript
  correct) before deleting the old one
- Teardown order that works: delete all object versions + delete markers
  (versioned bucket), batch-delete ECR images, then `delete-stack`. Old
  DynamoDB job history was dropped with the stack (transcripts live in S3).
- NOT renamed: nothing — this was the full rename. The old API endpoint and
  job IDs are dead; the iOS Shortcut needs the new endpoint URL.

### Local mode: shared core + second entrypoint (2026-06-08)
Added a laptop-only path (download + transcribe, no AWS) without forking the
codebase. The design principle: **same code, two doors — no runtime
local-vs-cloud detection.** The environment is decided purely by which
entrypoint is invoked.
- Extracted the transcription + paragraph-formatting logic out of
  `lambda_handler.py` into a new AWS-free `transcribe_core.py`
  (`whisper_segments`, `format_timestamped`, `format_paragraphs`). The Lambda
  handler now imports them — a pure relocation, so cloud behavior is unchanged.
- One deliberate behavior tweak in `get_model`: the model id now defaults to
  `"base"` (faster-whisper auto-downloads) when `WHISPER_MODEL_PATH` is unset.
  In Lambda that env var is always set (Dockerfile + CFN), so the cloud still
  uses the baked `/opt/whisper-models/base`; locally it falls back to the
  download. No cloud change.
- New `transcribe_local.py`: reuses `AudioDownloader` (already local-capable)
  for the download, then `transcribe_core` for transcription, writing
  `<title>.txt` + `<title>-clean.txt` to a folder. **No chunking** — a laptop
  has no 15-min deadline, so it transcribes whole files.
- Requirements split: `requirements.txt` left untouched (zero cloud risk);
  new `requirements-local.txt` adds faster-whisper, omits boto3.
- Dockerfile gotcha: the `COPY` line had to add `transcribe_core.py` or the
  handler's new import would fail at cold start. This is the one change that
  could have broken cloud — covered by redeploy + a live "Me at the zoo" API
  smoke test before committing.
- Verified: local CLI on "Me at the zoo" → correct transcript; cloud redeploy
  + live API run still reaches `done`.

### GitHub-issue bridge for the Claude iOS app (2026-06-08)
The consumer Claude app can't call the private AWS endpoint (its skill sandbox
isn't built to make authenticated calls to your own API), but it *can* drive a
**GitHub connector**. So GitHub became the broker — no AWS changes at all, the
Action only *calls* the existing API.
- `.github/workflows/transcribe-request.yml`: triggers on an issue titled
  `transcribe: <url>`, extracts the URL, POSTs to the AWS API (token +
  endpoint as repo secrets `V2T_API_TOKEN` / `V2T_API_URL`), polls up to ~15
  min, then writes the clean transcript into the issue body and flips the title
  to `Transcript: <url>` (or `Transcribe FAILED: <url>`).
- Why GitHub-side polling (not AWS posting back): keeps the AWS stack 100%
  untouched — the whole feature is one workflow + two secrets. Lower risk.
- This path inherits AWS's cookie auth, so it handles YouTube/IG/FB — unlike
  the pure-Actions x-video-transcribe skill, whose runner IPs YouTube blocks.
- Client side (`docs/claude-app-setup.md`): enable the GitHub connector in the
  Claude app + a Claude Project whose instructions encode the
  `transcribe:`/`Transcript:` issue convention. The app creates the issue and
  reads the result; the token never touches the phone.
- Verified end-to-end: opened a real `transcribe:` issue for "Me at the zoo" →
  Action ran → issue body came back with the correct transcript, title flipped.
- Token rotation after a redeploy:
  `printf %s "$(cat .api-token)" | gh secret set V2T_API_TOKEN --repo amroja-biz/video-to-transcript`.

### Claude Code mobile: the third client — and why it needed a guardrail (2026-06-08)
The GitHub-issue bridge was built for the *consumer Claude app*, but the more
natural phone client turned out to be **Claude Code mobile**, which already has
a working GitHub connection (the same one the `x-video-transcribe` skill uses
from a phone). The consumer-app route was a dead end here: that app has **no
GitHub connector** to enable — its tool search surfaces nothing, and it tries to
fall back to a `gh` CLI that doesn't exist on the phone. So the whole Claude
Project + connector setup in `docs/claude-app-setup.md` doesn't apply to this
user; Claude Code mobile is the path.

The trap: Claude Code mobile runs in a **cloud sandbox that clones the repo**,
and the repo *looks like* an installable app (Dockerfile, requirements.txt). So
on "transcribe this," it did the natural thing — cloned, built a container, and
tried to `pip install` the app — which can't work (the sandbox can't reach the
source sites and has no GPU). Crucially, the sandbox does **not** see the
laptop's `~/.claude/skills/`, so a personal skill couldn't fix it; only files
**committed in the repo** reach the clone.

Fix (committed, so the clone carries it):
- A hard guardrail at the top of `CLAUDE.md` (always loaded from the clone):
  if asked to transcribe, do **not** clone-and-run / build / `pip install` —
  create a `transcribe: <url>` issue via the `mcp__github__*` tools, poll it,
  read the transcript back. This is what actually changed the behavior, because
  CLAUDE.md is in context unconditionally.
- A project skill `.claude/skills/video-transcribe/SKILL.md` with the full
  procedure (create issue → poll every ~60–90s up to ~16 min → present verbatim
  with paragraph breaks). Hardcodes `owner=amroja-biz repo=video-to-transcript`
  since there's no git remote to discover from in a phone-driven flow, and uses
  the GitHub MCP tools rather than `gh` (unauthenticated in the sandbox).
- Lesson: for Claude Code mobile, behavior is steered by what's **committed** —
  CLAUDE.md first, then in-repo skills. Laptop-only personal skills are invisible
  to the cloud sandbox.
- Verified end-to-end from the phone: "transcribe `<url>`" → skipped the install
  path → created issue #2 → AWS pipeline ran → transcript returned in the issue.

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
