---
name: video-transcribe
description: Transcribe a video from a URL (YouTube, Instagram, Facebook, X.com, and other yt-dlp-supported sites) and return a clean, readable transcript. Use whenever the user asks to "transcribe", "get the words from", or "give me the transcript of" a video link that is NOT solely an x.com link (for x.com-only, the x-video-transcribe skill also applies). Do the work by creating a GitHub issue in amroja-biz/video-to-transcript — a GitHub Action there hands the URL to the project's AWS pipeline and writes the transcript back into the issue. NEVER download the video or install yt-dlp/whisper/ffmpeg locally; Claude's sandbox blocks the source sites and has no GPU. Always use the GitHub-issue bridge.
---

# video-transcribe

Transcription runs on a server-side AWS pipeline. You drive it purely through
**GitHub issues** — no repo checkout, no git, no local downloading. This works
from the Claude mobile app because it only needs the GitHub connection (the
`mcp__github__*` tools), exactly like the `x-video-transcribe` skill.

**Do NOT** attempt to download the video or `pip install yt-dlp` / whisper /
ffmpeg. The sandbox can't reach the source sites and has no GPU — it will fail.
The only correct path is the issue bridge below.

## The bridge

```
create issue "transcribe: <url>"  ──▶ GitHub (amroja-biz/video-to-transcript)
                                        │ Action: transcribe-request.yml
                                        ▼
                     POST /downloads ──▶ AWS pipeline (yt-dlp + whisper)
                     poll until done ◀──
                                        │ writes transcript into the issue body,
                                        │ title → "Transcript: <url>"        (ok)
                                        │ title → "Transcribe FAILED: <url>" (error)
read issue body  ◀──────────────────────
```

Fixed target repo (hardcoded — there is no local checkout to discover it from):

- **owner** = `amroja-biz`
- **repo**  = `video-to-transcript`

The Action fires on any issue whose title starts `transcribe:` (case-
insensitive). The AWS endpoint + token are GitHub Actions secrets; nothing
sensitive is needed from the phone.

## Tools

Use the **GitHub MCP tools** (`mcp__github__*`) — create-issue to submit,
get/read-issue to poll. (Not the `gh` CLI; it isn't authenticated in the mobile
sandbox.) If the GitHub tools aren't loaded, find them with ToolSearch
(query `github issue`). If GitHub truly isn't connected, tell the user to enable
the GitHub connection in the Claude app — the same one the X transcriber uses.

## Procedure

When the user supplies a video URL:

1. **Create the request issue** via the GitHub MCP create-issue tool:
   - `owner` = `amroja-biz`, `repo` = `video-to-transcript`
   - `title` = `transcribe: <the exact URL>`
   - `body`  = `<the exact URL>`

   Record the returned issue **number**; tell the user it's processing
   (~1–6 min typically; long videos up to ~15).

2. **Wait, then poll.** Use `Monitor` with `sleep 90 && echo checkpoint=tick`
   (timeout `120000ms`) for the first wait, then re-read the issue. Re-read
   every ~60–90s. Don't poll faster — the Action submits to AWS and polls it for
   up to ~15 minutes. Give up after ~16 minutes and report a timeout.

3. **Read the issue** (GitHub MCP get/read-issue, owner `amroja-biz`, repo
   `video-to-transcript`, the number from step 1) and branch on the **title**:
   - Starts with **`Transcript:`** → success; transcript is in the body after
     the `Transcribed from: <url>` line. Go to step 4.
   - Starts with **`Transcribe FAILED:`** → tell the user it failed and show the
     `Error:` line from the body. Stop.
   - Still starts with **`transcribe:`** → not ready; wait and re-read.

4. **Present the transcript** as plain markdown paragraphs. Take everything after
   the `Transcribed from:` line (and the blank line under it) and insert
   paragraph breaks at natural boundaries to make it readable:
   - Topic shifts ("So...", "And then...", "Now let's...").
   - The end of an extended thought before the speaker pivots.

   **Hard rules (verbatim):**
   - Do **not** change, add, delete, paraphrase, summarize, or "correct" any
     words. Text between breaks must be byte-identical to a contiguous slice of
     the issue body.
   - No headings, bullets, or bold — plain paragraphs only. A break is just
     `\n\n` at an existing word boundary.
   - If the body ends with `_(transcript truncated …)_`, surface that note.

## Failure modes

- **Tempted to install yt-dlp / download the video.** Wrong path — stop and use
  the issue bridge. The sandbox blocks the source sites and has no GPU.
- **No GitHub MCP tools.** ToolSearch (`github issue`). If genuinely absent,
  tell the user to enable the GitHub connection in the Claude app.
- **`Transcribe FAILED: no URL found`.** The title didn't carry a valid URL.
  Recreate the issue with the link directly after `transcribe:` in the title.
- **`Transcribe FAILED: <url>` with a yt-dlp error.** Source likely private/
  geo-blocked, or YouTube is throttling and the project's Docker image needs a
  yt-dlp bump. Report the error; don't retry blindly.
- **Timed out after ~15 min.** The Action hit the pipeline's hard deadline (very
  long video or a stuck job). Tell the user; they can retry.

## Notes

- Supported sources: YouTube, Instagram, Facebook, X.com, and other
  yt-dlp-supported sites (this pipeline uses stored browser cookies, so unlike
  the GitHub-runner-only `x-video-transcribe` skill it reaches YouTube).
- One URL per issue. For multiple URLs, repeat the procedure per URL.
