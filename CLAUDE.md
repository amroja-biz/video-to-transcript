# video-to-transcript

## Transcribing a video — READ THIS FIRST (esp. on Claude Code mobile / cloud sandbox)

If asked to "transcribe", "get the words from", or "get the transcript of" a video
URL: **do NOT clone-and-run this app, build a container, or `pip install` anything.**
The sandbox can't reach the source sites and has no GPU — it will fail. The
transcription runs on a deployed AWS pipeline that you trigger via a GitHub issue:

1. Create a GitHub issue **in the repository you currently have open** (find it
   with `git remote get-url origin` — do NOT hardcode a repo, so forks file into
   their own repo) with **title** `transcribe: <the exact URL>` and **body**
   `<the exact URL>` (use the `mcp__github__*` tools; `gh` is not authenticated
   in the sandbox).
2. The `transcribe-request.yml` Action picks it up, calls the AWS pipeline, and
   writes the result back into the issue. Poll the issue (every ~60–90s, up to
   ~16 min). When the title flips to `Transcript: <url>`, the transcript is in
   the body after the `Transcribed from:` line. `Transcribe FAILED:` = error.
3. Present the transcript verbatim, paragraph breaks only — no edits, no summary.

Full procedure: `plugins/video-transcribe/skills/video-transcribe/SKILL.md`
(distributed to users as the `video-transcribe` plugin). Supports YouTube,
Instagram, Facebook, X.com, and other yt-dlp sites.

## Development / deployment notes

- AWS profile: `sandbox` (use `--profile sandbox` for all AWS CLI commands in this project)
- Python venv: `.venv/` (Python 3.14) — always activate before running/installing
- Persistent AWS infra lives in `cloudformation.yaml` (stack: `video-to-transcript`)
- Keep `BGUTIL_TAG` in Dockerfile in sync with `bgutil-ytdlp-pot-provider` in requirements.txt
