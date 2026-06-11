---
name: video-transcribe
description: Transcribe a video from a URL (YouTube, Instagram, Facebook, X.com, and other yt-dlp-supported sites) and return a clean, readable transcript. Use whenever the user asks to "transcribe", "get the words from", or "give me the transcript of" a video link that is NOT solely an x.com link (for x.com-only, the x-video-transcribe skill also applies). Do the work by creating a GitHub issue in the user's request repo — a GitHub Action there (transcribe-request.yml) hands the URL to an AWS pipeline and writes the transcript back into the issue. NEVER download the video or install yt-dlp/whisper/ffmpeg locally, and never try to transcribe it yourself; Claude's sandbox blocks the source sites and has no GPU. Always use the GitHub-issue bridge.
---

# video-transcribe

You transcribe by creating a **GitHub issue in the user's request repo**; a
GitHub Action in that repo does the actual work on AWS and writes the result
back into the issue, which you then read.

**Do NOT** download the video, `pip install yt-dlp` / whisper / ffmpeg, build a
container, or try to transcribe the audio yourself. The sandbox can't reach the
source sites and has no GPU — it will fail, and that is not the job. The only
correct path is the issue bridge below.

## The bridge

```
create issue "transcribe: <url>"  ──▶ GitHub (user's request repo)
                                        │ Action: transcribe-request.yml
                                        ▼
                     POST /downloads ──▶ AWS pipeline (yt-dlp + whisper)
                     poll until done ◀──
                                        │ writes transcript into the issue body,
                                        │ title → "Transcript: <url>"        (ok)
                                        │ title → "Transcribe FAILED: <url>" (error)
read issue body  ◀──────────────────────
```

The Action fires on any issue whose title starts `transcribe:` (case-
insensitive). The AWS endpoint + token are GitHub Actions secrets in that repo;
nothing sensitive is needed at request time.

## Pre-flight: which repo

File the issue in the user's **request repo** (the repo they set up with the
`transcribe-request.yml` workflow and the `V2T_API_URL` / `V2T_API_TOKEN`
secrets). Determine `<owner>/<repo>` in this order:

1. The repo the user named for this request (e.g. "transcribe X in acme/clips").
2. A request repo they configured earlier this session or in their memory/notes.
3. Otherwise **ask** the user once: "Which GitHub repo should I file transcribe
   requests in (`owner/repo`)?" Use that for every GitHub call below.

## Tools

Use the **GitHub MCP tools** (`mcp__github__*`) — create-issue to submit,
get/read-issue to poll. (Not the `gh` CLI; it isn't authenticated in the mobile
sandbox.) If the GitHub tools aren't loaded, find them with ToolSearch
(query `github issue`). If GitHub truly isn't connected, tell the user to grant
the Claude GitHub app access to their request repo.

## Procedure

When the user supplies a video URL:

1. **Create the request issue** via the GitHub MCP create-issue tool:
   - `owner` / `repo` = the values from pre-flight
   - `title` = `transcribe: <the exact URL>`
   - `body`  = `<the exact URL>`

   Record the returned issue **number**; tell the user it's processing
   (~1–6 min typically; long videos up to ~15).

2. **Wait, then poll.** Use `Monitor` with `sleep 90 && echo checkpoint=tick`
   (timeout `120000ms`) for the first wait, then re-read the issue. Re-read
   every ~60–90s. Don't poll faster — the Action submits to AWS and polls it for
   up to ~15 minutes. Give up after ~16 minutes and report a timeout.

3. **Read the issue** (GitHub MCP get/read-issue, same `owner`/`repo`, the
   number from step 1) and branch on the **title**:
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

- **Tempted to install yt-dlp / download the video / transcribe it yourself.**
  Wrong path — stop and use the issue bridge. The sandbox blocks the source
  sites and has no GPU.
- **No GitHub MCP tools.** ToolSearch (`github issue`). If genuinely absent,
  tell the user to grant the Claude GitHub app access to their request repo.
- **`Transcribe FAILED: no URL found`.** The title didn't carry a valid URL.
  Recreate the issue with the link directly after `transcribe:` in the title.
- **`Transcribe FAILED: <url>` with a yt-dlp error.** Source likely private/
  geo-blocked, or YouTube is throttling and the project's Docker image needs a
  yt-dlp bump. Report the error; don't retry blindly.
- **Timed out after ~15 min.** The Action hit the pipeline's hard deadline (very
  long video or a stuck job). Tell the user; they can retry.

## Notes

- Supported sources: YouTube, Instagram, Facebook, X.com, and other
  yt-dlp-supported sites (the AWS pipeline uses stored browser cookies, so unlike
  the GitHub-runner-only `x-video-transcribe` skill it reaches YouTube).
- One URL per issue. For multiple URLs, repeat the procedure per URL.
- This requires the repo to have the `transcribe-request.yml` workflow and the
  `V2T_API_URL` / `V2T_API_TOKEN` secrets set (see the project README).
