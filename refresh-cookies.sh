#!/usr/bin/env bash
# One-command cookie refresh: export the full Chrome cookie jar and upload
# to S3 where the AWS container picks it up.
#
# Usage:
#   AUDIO_DL_BUCKET=<bucket> ./refresh-cookies.sh
#   ./refresh-cookies.sh -b <bucket> [-p <aws-profile>] [-B <browser>]
#
# Notes:
#  - First run on macOS prompts for Keychain access (Chrome cookie decryption).
#  - Visiting youtube.com just anchors the export; the WHOLE jar is written,
#    so Instagram/Facebook/X cookies come along too.
set -euo pipefail

cd "$(dirname "$0")"

BUCKET="${AUDIO_DL_BUCKET:-amroja-audio}"
PROFILE="${AWS_PROFILE:-sandbox}"
BROWSER="chrome"

while getopts "b:p:B:" opt; do
  case $opt in
    b) BUCKET="$OPTARG" ;;
    p) PROFILE="$OPTARG" ;;
    B) BROWSER="$OPTARG" ;;
    *) echo "Usage: $0 [-b bucket] [-p aws-profile] [-B browser]" >&2; exit 2 ;;
  esac
done

YTDLP=".venv/bin/yt-dlp"
if [[ ! -x "$YTDLP" ]]; then
  echo "✗ $YTDLP not found. Create the venv first (see README)." >&2
  exit 2
fi

# yt-dlp loads --cookies before writing it, so the path must not pre-exist.
TMPDIR_="$(mktemp -d)"
TMP="$TMPDIR_/cookies.txt"
trap 'rm -rf "$TMPDIR_"' EXIT
chmod 700 "$TMPDIR_"

echo "Exporting cookies from $BROWSER ..."
# --skip-download: we only want the cookie jar written to $TMP.
"$YTDLP" --cookies-from-browser "$BROWSER" --cookies "$TMP" \
  --skip-download --no-warnings "https://www.youtube.com" >/dev/null || true

if [[ ! -s "$TMP" ]]; then
  echo "✗ Cookie export produced an empty file. Is $BROWSER installed and signed in?" >&2
  exit 1
fi

DEST="s3://$BUCKET/cookies/cookies.txt"
echo "Uploading to $DEST (profile: $PROFILE) ..."
aws s3 cp "$TMP" "$DEST" --sse AES256 --profile "$PROFILE" >/dev/null

echo "✓ Cookies refreshed: $DEST"
