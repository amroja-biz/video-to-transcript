# Transcribe from the Claude iOS app (no Shortcut)

This sets up transcription entirely inside the **Claude app** on your phone. You
paste a video URL, Claude files a GitHub issue, a GitHub Action hands the URL to
the AWS pipeline, the transcript comes back into the issue, and Claude reads it
to you. The app only ever talks to **GitHub** — never to AWS — and your API
token stays server-side as a GitHub secret.

The server side is already deployed (`.github/workflows/transcribe-request.yml`
+ the `V2T_API_URL` / `V2T_API_TOKEN` repo secrets). You only need to do the two
one-time steps below.

## Step 1 — Connect GitHub in the Claude app

1. In the Claude app: **Settings → Connectors** (a.k.a. Integrations).
2. Add / enable the **GitHub** connector and sign in to the GitHub account that
   can access `amroja-biz/video-to-transcript`.
3. Make sure it has permission to **create and read issues** (not read-only). If
   GitHub asks which repositories to grant, include
   `amroja-biz/video-to-transcript`.

> If the connector turns out to be read-only and can't create issues, tell me —
> there's a fallback (a one-action Shortcut submits; everything else stays the
> same).

## Step 2 — Create a Claude Project with these instructions

1. In the Claude app, create a new **Project** named e.g. "Video Transcriber".
2. Paste the following into the Project's **custom instructions**:

```
You transcribe videos for me through a GitHub-backed pipeline.
Repository: amroja-biz/video-to-transcript

WHEN I GIVE YOU A VIDEO URL:
1. Create a GitHub issue in amroja-biz/video-to-transcript with:
   - Title:  transcribe: <the exact URL>
   - Body:   <the exact URL>
2. Tell me the issue number and that it's processing (it takes ~1–6 minutes;
   long videos a bit more).

WHEN I ASK FOR THE RESULT (or "is it ready?"):
- Read that issue.
- If the title now starts with "Transcript:", the transcript is in the issue
  body. Present everything after the "Transcribed from:" line, verbatim, split
  into readable paragraphs.
- If the title starts with "Transcribe FAILED:", tell me it failed and show the
  error from the body.
- If the title still starts with "transcribe:", it isn't ready yet — wait about
  30 seconds and check again.

RULES:
- Never change, add, remove, or paraphrase any words of the transcript. Only
  insert paragraph breaks for readability.
- Works for YouTube, Instagram, Facebook, X.com, and other supported sites.
```

## Using it

In that Project, on your phone:

- **"Transcribe https://youtube.com/watch?v=…"** → Claude files the issue and
  tells you the number.
- A minute or two later: **"Is it ready?"** → Claude reads the issue and shows
  you the transcript.

That's it — no Shortcuts app, no copying tokens, nothing leaves GitHub except the
call the Action makes to your own pipeline.

## How it works (for reference)

```
Claude app ──create issue "transcribe: <url>"──▶ GitHub
                                                   │ Action: transcribe-request.yml
                                                   ▼
                              POST /downloads ──▶ AWS pipeline (download + whisper)
                              poll until done ◀──
                                                   │ writes transcript into the issue,
                                                   │ title → "Transcript: <url>"
Claude app ◀──read issue body──────────────────────
```

The token (`V2T_API_TOKEN`) and endpoint (`V2T_API_URL`) are GitHub Actions
secrets in the repo. To rotate the token after a redeploy:
`printf %s "$(cat .api-token)" | gh secret set V2T_API_TOKEN --repo amroja-biz/video-to-transcript`.
