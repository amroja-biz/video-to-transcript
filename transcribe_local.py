#!/usr/bin/env python3
"""transcribe_local: laptop-only video → transcript. No AWS.

Downloads audio from any yt-dlp-supported URL (YouTube, Instagram, Facebook,
X.com, ...) using your browser cookies, transcribes it with faster-whisper, and
writes the transcript next to the audio. This is the local door into the same
shared core the cloud pipeline uses (video_to_transcript.AudioDownloader for the
download, transcribe_core for the transcription/formatting) — it just keeps
everything on disk instead of S3/DynamoDB.

Unlike the cloud path there is no chunking: a laptop has no 15-minute deadline,
so each file is transcribed whole.

Usage:
    python transcribe_local.py URL [URL ...]
    python transcribe_local.py -o ~/transcripts URL
    python transcribe_local.py --model small URL
    python transcribe_local.py --no-keep-audio URL          # delete the MP3 after

Requirements: pip install -r requirements-local.txt  (plus ffmpeg, and deno for
YouTube). The first run downloads the whisper model (~150 MB for "base").
"""

import argparse
import sys
from argparse import Namespace

from video_to_transcript import AudioDownloader
from transcribe_core import whisper_segments, format_timestamped, format_paragraphs


def transcribe_file(mp3_path, model=None):
    """Transcribe one local audio file; write <stem>.txt and <stem>-clean.txt
    beside it. Returns (timestamped_path, clean_path)."""
    segments = whisper_segments(mp3_path, model=model)
    ts_path = mp3_path.with_suffix("").with_name(mp3_path.stem + ".txt")
    clean_path = mp3_path.with_name(mp3_path.stem + "-clean.txt")
    ts_path.write_text(format_timestamped(segments), encoding="utf-8")
    clean_path.write_text(format_paragraphs(segments), encoding="utf-8")
    return ts_path, clean_path


def main():
    parser = argparse.ArgumentParser(
        description="Download audio and transcribe it locally (no AWS). "
                    "Supports YouTube, Instagram, Facebook, X.com, etc.",
    )
    parser.add_argument("urls", nargs="+", help="One or more video/audio URLs")
    parser.add_argument(
        "-o", "--output-dir", default="transcripts",
        help="Output directory (a YYYY-MM-DD subdir is created; default: transcripts)",
    )
    parser.add_argument(
        "--model", default="base",
        help="faster-whisper model: tiny, base, small, medium, large-v3, or a "
             "local model path (default: base; downloaded on first use)",
    )
    parser.add_argument("--cookies", help="Path to a Netscape-format cookies.txt")
    parser.add_argument(
        "--cookies-from-browser", metavar="BROWSER", default=None,
        help="Browser to read cookies from (chrome, firefox, safari, ...), or "
             "'none' to disable. Default: chrome.",
    )
    parser.add_argument(
        "--keep-audio", action="store_true", default=True,
        help="Keep the downloaded MP3 (default).",
    )
    parser.add_argument(
        "--no-keep-audio", dest="keep_audio", action="store_false",
        help="Delete the MP3 after transcribing; keep only the .txt files.",
    )
    parser.add_argument(
        "--audio-quality", default="0",
        help="ffmpeg mp3 quality: 0 (best VBR) to 9, or a bitrate like 192K (default: 0)",
    )
    parser.add_argument(
        "--pot-server", metavar="URL", default=None,
        help="bgutil PO-token server URL (optional; only helps stubborn YouTube)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose yt-dlp output")
    args = parser.parse_args()

    # AudioDownloader takes the same Namespace shape the Lambda worker builds;
    # s3_output=None keeps everything local.
    dl_args = Namespace(
        output_dir=args.output_dir,
        cookies=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        s3_output=None,
        keep_local=True,
        audio_quality=args.audio_quality,
        pot_server=args.pot_server,
        verbose=args.verbose,
    )
    dl = AudioDownloader(dl_args)

    succeeded, failed = [], []
    try:
        for url in args.urls:
            print(f"\n→ {url}")
            mp3 = dl.download(url)
            if mp3 is None:
                failed.append(url)
                continue
            print("Transcribing (this can take a while on first run) ...")
            try:
                ts_path, clean_path = transcribe_file(mp3, model=args.model)
            except Exception as e:
                print(f"✗ Transcription failed for {url}: {e}", file=sys.stderr)
                failed.append(url)
                continue
            if not args.keep_audio:
                mp3.unlink(missing_ok=True)
            print(f"✓ Transcript: {clean_path}")
            print(f"  Timestamped: {ts_path}")
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
