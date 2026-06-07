# video-to-transcript

- AWS profile: `sandbox` (use `--profile sandbox` for all AWS CLI commands in this project)
- Python venv: `.venv/` (Python 3.14) — always activate before running/installing
- Persistent AWS infra lives in `cloudformation.yaml` (stack: `audio-downloader`)
- Keep `BGUTIL_TAG` in Dockerfile in sync with `bgutil-ytdlp-pot-provider` in requirements.txt
