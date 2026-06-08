# Plan: Design A — transcribe from the Claude iOS app via a GitHub issue bridge

**Goal:** transcribe a video URL entirely from the **Claude iOS app**, with no
Shortcut and without the app ever calling the private AWS endpoint. The app only
talks to **GitHub** (a supported connector); GitHub brokers to AWS.

**Hard constraint: the working AWS stack is not touched.** The GitHub Action
*calls* the existing API; no CloudFormation / Lambda / Step Functions changes.

## Flow

```
Claude iOS app ──(GitHub connector: create issue "transcribe: <url>")──▶ GitHub
                                                                          │ on: issues opened
                                                                          ▼
                                       GitHub Action (this repo)
                                         • reads URL from the issue title
                                         • POST /downloads to AWS  (token = GH secret)
                                         • polls GET /downloads/<id> until done
                                         • writes transcript into the issue body,
                                           renames title → "Transcript: <url>"
                                                                          │
Claude iOS app ◀──(GitHub connector: read issue body)─────────────────────
```

The AWS token lives only as a **GitHub Actions secret** — never on the phone.
Because AWS does the actual download (with its cookie auth), this path supports
YouTube/IG/FB, which the pure-GitHub-Actions x-video-transcribe skill can't.

## Build (server side — I do this)

1. **`.github/workflows/transcribe-request.yml`**
   - `on: issues: types: [opened]`, gated to titles starting `transcribe:`
     (also `Transcribe:`).
   - `permissions: issues: write`.
   - Steps: extract URL from title (fallback: first http(s) token in body) →
     POST to AWS, poll up to ~15 min → on `done`, fetch the full clean transcript
     via `transcript_clean_url` and write it into the issue body, rename title to
     `Transcript: <url>`; on `error`/timeout, rename to `Transcribe FAILED: <url>`
     and put the error in the body.
   - Comment bodies are capped at 65 535 chars; truncate very long transcripts
     with a note (typical clips fit — a 40-min talk was ~40 KB).
2. **Repo secrets** (`gh secret set`, token read from `.api-token` without
   printing): `V2T_API_URL`, `V2T_API_TOKEN`.
3. Commit + push the workflow.
4. **Test**: `gh issue create --title "transcribe: <youtube url>"`, watch the run,
   confirm the issue body ends up with the transcript and the title flips.

## Set up (client side — user does, I provide text)

5. **`docs/claude-app-setup.md`** — exact steps:
   - Enable the **GitHub connector** in the Claude iOS app (must allow creating
     issues, not just reading).
   - Create a Claude **Project** and paste the provided custom instructions
     (repo name, `transcribe: <url>` convention, how to read the result).
6. Usage: in that Project, "transcribe <url>" → Claude files the issue; "is it
   ready?" → Claude reads it back.

## Repo choice
Use the existing `amroja-biz/video-to-transcript` repo (workflow + issues +
secrets in one place). Transcript-request issues are distinguishable by the
`transcribe:` / `Transcript:` title prefix. Can split to a dedicated inbox repo
later if issue noise becomes annoying.

## Dependency to verify
The Claude iOS app's GitHub connector must be able to **create** issues. If it's
read-only, fall back to Design B (tiny Shortcut submits; AWS/Action posts to an
issue; Claude reads it).

## Out of scope
- No AWS changes. No Shortcut. (Optional later: a Claude Code skill for the Mac.)
