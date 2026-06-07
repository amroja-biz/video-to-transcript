#!/usr/bin/env python3
"""audio-downloader: CLI-only, audio-only (MP3) downloader.

Downloads audio from YouTube, Instagram, Facebook, X.com, and any other
site yt-dlp supports. Runs locally (uses your browser cookies) or in a
Docker container on AWS (pulls cookies from S3, can upload results to S3).

Usage:
    python audio_downloader.py URL [URL ...]
    python audio_downloader.py --cookies cookies.txt URL
    python audio_downloader.py --s3-output s3://bucket/audio/ URL

Environment variables (used by the AWS/container path):
    AUDIO_DL_COOKIES_S3   s3://bucket/cookies/cookies.txt  (cookie source)
    AUDIO_DL_S3_OUTPUT    s3://bucket/audio/               (where MP3s land)
    AUDIO_DL_POT_SERVER   http://127.0.0.1:4416            (bgutil POT server)
    AUDIO_DL_IN_CONTAINER set to 1 inside Docker (disables browser cookies)
"""

import argparse
import os
import re
import socket
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

DEFAULT_POT_SERVER = "http://127.0.0.1:4416"
COOKIE_REFRESH_HINT = (
    "If this is an auth/bot-check error, your cookies are missing or expired — "
    "run ./refresh-cookies.sh and try again."
)


def parse_s3_uri(uri):
    """Split s3://bucket/prefix into (bucket, prefix). Exits 2 on bad URI."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        print(f"✗ Invalid S3 URI: {uri} (expected s3://bucket/prefix)", file=sys.stderr)
        sys.exit(2)
    return parsed.netloc, parsed.path.lstrip("/")


def s3_client():
    """Lazy boto3 import so local runs don't need AWS creds or boto3 setup."""
    try:
        import boto3
    except ImportError:
        print("✗ boto3 is required for S3 features: pip install boto3", file=sys.stderr)
        sys.exit(2)
    return boto3.client("s3")


def pot_server_reachable(url, timeout=1.0):
    """Probe the bgutil POT server with a quick TCP connect."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class AudioDownloader:
    def __init__(self, args):
        self.args = args
        self.in_container = os.environ.get("AUDIO_DL_IN_CONTAINER") == "1"
        self.tmp_cookie_file = None
        self.last_info = None   # yt-dlp info dict from the most recent download
        self.last_error = None  # error string from the most recent failure

        date_dir = datetime.now().strftime("%Y-%m-%d")
        self.out_dir = Path(args.output_dir) / date_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.s3_output = args.s3_output or os.environ.get("AUDIO_DL_S3_OUTPUT")
        if self.s3_output:
            self.s3_bucket, self.s3_prefix = parse_s3_uri(self.s3_output)

        self.ydl_opts = self._build_ydl_opts()

    # ---------- cookies ----------

    def _resolve_cookies(self, opts):
        """Apply cookie source in priority order:
        --cookies file > AUDIO_DL_COOKIES_S3 > browser (local only) > anonymous.
        """
        if self.args.cookies:
            path = Path(self.args.cookies)
            if not path.is_file():
                print(f"✗ Cookies file not found: {path}", file=sys.stderr)
                sys.exit(2)
            opts["cookiefile"] = str(path)
            print(f"Using cookies file: {path}")
            return

        cookies_s3 = os.environ.get("AUDIO_DL_COOKIES_S3")
        if cookies_s3:
            bucket, key = parse_s3_uri(cookies_s3)
            fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="cookies-")
            os.close(fd)
            os.chmod(tmp_path, 0o600)
            try:
                s3_client().download_file(bucket, key, tmp_path)
            except Exception as e:
                print(f"✗ Failed to fetch cookies from {cookies_s3}: {e}", file=sys.stderr)
                print("  Run ./refresh-cookies.sh locally to (re)upload them.", file=sys.stderr)
                sys.exit(2)
            self.tmp_cookie_file = tmp_path
            opts["cookiefile"] = tmp_path
            print(f"Using cookies from {cookies_s3}")
            return

        browser = self.args.cookies_from_browser
        if browser is None and not self.in_container:
            browser = "chrome"  # sensible local default, matches old yt-downloader
        if browser and browser.lower() != "none":
            opts["cookiesfrombrowser"] = (browser,)
            print(f"Using cookies from browser: {browser}")
            return

        print("⚠ No cookies configured — YouTube/Instagram/Facebook may refuse downloads.")

    # ---------- yt-dlp options ----------

    def _build_ydl_opts(self):
        opts = {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": self.args.audio_quality,
                }
            ],
            "outtmpl": str(self.out_dir / "%(uploader)s_%(title)s_%(id)s.%(ext)s"),
            "noplaylist": True,
            "cachedir": os.environ.get("AUDIO_DL_CACHE_DIR") or None,
            "quiet": not self.args.verbose,
            "no_warnings": not self.args.verbose,
            "progress": True,
        }
        self._resolve_cookies(opts)

        pot_server = (
            self.args.pot_server
            or os.environ.get("AUDIO_DL_POT_SERVER")
            or DEFAULT_POT_SERVER
        )
        if pot_server_reachable(pot_server):
            opts["extractor_args"] = {
                "youtubepot-bgutilhttp": {"base_url": [pot_server]}
            }
            print(f"PO-token server detected at {pot_server}")
        return opts

    # ---------- download ----------

    def download(self, url):
        """Download one URL. Returns the final MP3 path, or None on failure."""
        self.last_info = None
        self.last_error = None
        try:
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                self.last_info = info
                # After FFmpegExtractAudio the real file is .mp3
                base = ydl.prepare_filename(info)
                mp3_path = Path(re.sub(r"\.[^.]+$", ".mp3", base))
                if not mp3_path.is_file():
                    self.last_error = f"Expected output not found: {mp3_path}"
                    print(f"✗ {self.last_error}", file=sys.stderr)
                    return None
                print(f"✓ Downloaded: {mp3_path}")
                return mp3_path
        except yt_dlp.utils.DownloadError as e:
            self.last_error = str(e)
            print(f"✗ Download failed for {url}: {e}", file=sys.stderr)
            print(f"  {COOKIE_REFRESH_HINT}", file=sys.stderr)
            return None
        except Exception as e:
            self.last_error = str(e)
            print(f"✗ Unexpected error for {url}: {e}", file=sys.stderr)
            return None

    def upload_to_s3(self, mp3_path):
        key = f"{self.s3_prefix.rstrip('/')}/{mp3_path.name}".lstrip("/")
        try:
            s3_client().upload_file(
                str(mp3_path),
                self.s3_bucket,
                key,
                ExtraArgs={"ContentType": "audio/mpeg"},
            )
        except Exception as e:
            print(f"✗ S3 upload failed for {mp3_path.name}: {e}", file=sys.stderr)
            return False
        print(f"✓ Uploaded: s3://{self.s3_bucket}/{key}")
        if self.in_container and not self.args.keep_local:
            mp3_path.unlink(missing_ok=True)
        return True

    def cleanup(self):
        if self.tmp_cookie_file:
            Path(self.tmp_cookie_file).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        description="Download audio (MP3) from YouTube, Instagram, Facebook, X.com, etc."
    )
    parser.add_argument("urls", nargs="+", help="One or more video/audio URLs")
    parser.add_argument(
        "-o", "--output-dir", default="downloads",
        help="Local output directory (a YYYY-MM-DD subdir is created; default: downloads)",
    )
    parser.add_argument("--cookies", help="Path to a Netscape-format cookies.txt")
    parser.add_argument(
        "--cookies-from-browser", metavar="BROWSER",
        help="Browser to read cookies from (chrome, firefox, safari, ...), or "
             "'none' to disable. Default: chrome when running locally; "
             "disabled in containers.",
    )
    parser.add_argument(
        "--s3-output", metavar="S3URI",
        help="Upload MP3s to this S3 prefix, e.g. s3://bucket/audio/ "
             "(or set AUDIO_DL_S3_OUTPUT)",
    )
    parser.add_argument(
        "--keep-local", action="store_true",
        help="Keep local files after S3 upload (container default is to delete)",
    )
    parser.add_argument(
        "--audio-quality", default="0",
        help="ffmpeg mp3 quality: 0 (best VBR) to 9, or a bitrate like 192K (default: 0)",
    )
    parser.add_argument(
        "--pot-server", metavar="URL",
        help=f"bgutil PO-token server URL (default: $AUDIO_DL_POT_SERVER or {DEFAULT_POT_SERVER})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose yt-dlp output")
    args = parser.parse_args()

    dl = AudioDownloader(args)
    succeeded, failed = [], []
    try:
        for url in args.urls:
            print(f"\n→ {url}")
            mp3 = dl.download(url)
            if mp3 is None:
                failed.append(url)
                continue
            if dl.s3_output and not dl.upload_to_s3(mp3):
                failed.append(url)
                continue
            succeeded.append(url)
    finally:
        dl.cleanup()

    print(f"\nDone: {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        for url in failed:
            print(f"  failed: {url}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
