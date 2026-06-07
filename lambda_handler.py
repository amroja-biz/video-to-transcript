"""Lambda worker for the audio-downloader Step Functions pipeline.

One container-image Lambda serves every compute state in the state machine;
the state machine passes {"step": "<name>", ...} and handler() dispatches:

    download          yt-dlp -> MP3 -> s3://<bucket>/audio/      (uses POT server)
    transcribe        whole-file faster-whisper -> transcripts/
    chunk             ffmpeg -f segment -> chunks/<id>/NNN.mp3
    transcribe_chunk  whisper one chunk -> chunks/<id>/NNN.json
    merge             chunk JSONs -> final transcripts/, cleanup

Status flow recorded in DynamoDB: queued -> downloading -> downloaded ->
transcribing -> done | error.

Lambda runtime notes: filesystem is read-only except /tmp and the process is
not root, so HOME/caches point at /tmp and the Deno module cache baked into
the image at /opt/deno-cache is copied to /tmp/deno-cache before the PO-token
server starts (Deno requires a WRITABLE cache dir even when fully pre-cached).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Writable-location plumbing must happen before yt_dlp/boto3 imports.
os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/.cache")
os.environ.setdefault("AUDIO_DL_CACHE_DIR", "/tmp/yt-dlp-cache")

import boto3

from audio_downloader import AudioDownloader, parse_s3_uri, pot_server_reachable

# ---- paragraph formatting tunables ----
PAUSE_BREAK_S = 1.5        # gap between segments that forces a paragraph break
MARKER_MIN_PARA_CHARS = 200   # discourse marker only breaks once para is this long
MAX_PARA_CHARS = 800       # runaway paragraph cap (break at next sentence end)
TOPIC_MARKERS = (
    "so ", "so,", "and then", "now ", "now,", "okay", "anyway",
    "but ", "next ", "alright", "all right",
)

CHUNK_SECONDS = 600        # 10-minute chunks for long audio
POT_SERVER = "http://127.0.0.1:4416"

TABLE_NAME = os.environ.get("TABLE_NAME", "")
TRANSCRIPTS_S3 = os.environ.get("AUDIO_DL_TRANSCRIPTS_S3", "")
AUDIO_S3 = os.environ.get("AUDIO_DL_S3_OUTPUT", "")

_table = None
_s3 = None
_pot_proc = None
_whisper_model = None  # cached across warm invokes


def _get_table():
    global _table
    if _table is None and TABLE_NAME:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _now():
    return datetime.now(timezone.utc).isoformat()


def _update(job_id, **attrs):
    """Update the job item; never let bookkeeping kill the pipeline."""
    table = _get_table()
    if table is None:
        return
    try:
        expr = ", ".join(f"#k{i} = :v{i}" for i in range(len(attrs)))
        table.update_item(
            Key={"id": job_id},
            UpdateExpression=f"SET {expr}",
            ExpressionAttributeNames={f"#k{i}": k for i, k in enumerate(attrs)},
            ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(attrs.values())},
        )
    except Exception as e:  # pragma: no cover
        print(f"⚠ DynamoDB update failed for {job_id}: {e}", file=sys.stderr)


# ---------- PO-token server (download step only) ----------

def _ensure_pot_server():
    """Start the bgutil PO-token server under Deno, Lambda-style (writable /tmp)."""
    global _pot_proc
    if _pot_proc is not None and _pot_proc.poll() is None and pot_server_reachable(POT_SERVER):
        return
    cache_src, cache_dst = "/opt/deno-cache", "/tmp/deno-cache"
    if os.path.isdir(cache_src) and not os.path.isdir(cache_dst):
        shutil.copytree(cache_src, cache_dst)
    env = {**os.environ, "DENO_DIR": cache_dst, "HOME": "/tmp"}
    log = open("/tmp/bgutil.log", "ab")
    _pot_proc = subprocess.Popen(
        ["deno", "run", "--allow-env", "--allow-net", "--allow-ffi=.",
         "--allow-read=.", "src/main.ts"],
        cwd="/opt/bgutil/server", env=env, stdout=log, stderr=log,
    )
    for _ in range(40):  # up to ~20s
        if pot_server_reachable(POT_SERVER):
            print("PO-token server up on :4416")
            return
        time.sleep(0.5)
    print("⚠ PO-token server did not start; YouTube may fail (see /tmp/bgutil.log)",
          file=sys.stderr)


# ---------- whisper ----------

def _get_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(
            os.environ.get("WHISPER_MODEL_PATH", "/opt/whisper-models/base"),
            device="cpu", compute_type="int8",
        )
    return _whisper_model


def _whisper_segments(path, offset_s=0.0):
    """Transcribe a file; return [{start, end, text}] with optional time offset."""
    segments, _info = _get_model().transcribe(str(path), beam_size=5)
    return [
        {"start": float(s.start) + offset_s,
         "end": float(s.end) + offset_s,
         "text": s.text.strip()}
        for s in segments
    ]


def _ts(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:05.2f}"


def _format_timestamped(segments):
    return "\n".join(f"[{_ts(s['start'])} -> {_ts(s['end'])}] {s['text']}"
                     for s in segments) + "\n"


def _format_paragraphs(segments):
    """Insert paragraph breaks at natural boundaries. Words are never altered —
    only \n\n is inserted between whisper segments. Break signals:
      (a) long pause (gap >= PAUSE_BREAK_S between segments),
      (b) topic-shift discourse marker starting a segment, once the current
          paragraph is long enough to plausibly be a complete thought,
      (c) runaway paragraph after sentence-final punctuation.
    """
    paragraphs, current = [], []
    cur_len = 0
    prev_end = None
    for seg in segments:
        text = seg["text"]
        if not text:
            continue
        breaks = False
        if current:
            gap = seg["start"] - prev_end if prev_end is not None else 0.0
            lowered = text.lower()
            prev_text = current[-1]
            if gap >= PAUSE_BREAK_S:
                breaks = True
            elif (cur_len >= MARKER_MIN_PARA_CHARS
                  and lowered.startswith(TOPIC_MARKERS)):
                breaks = True
            elif cur_len > MAX_PARA_CHARS and prev_text.rstrip()[-1:] in ".?!":
                breaks = True
        if breaks:
            paragraphs.append(" ".join(current))
            current, cur_len = [], 0
        current.append(text)
        cur_len += len(text) + 1
        prev_end = seg["end"]
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) + "\n"


def _upload_transcripts(base_name, segments):
    """Upload timestamped + clean transcripts; return their S3 keys."""
    bucket, prefix = parse_s3_uri(TRANSCRIPTS_S3)
    prefix = prefix.rstrip("/")
    ts_key = f"{prefix}/{base_name}.txt"
    clean_key = f"{prefix}/{base_name}-clean.txt"
    _get_s3().put_object(Bucket=bucket, Key=ts_key,
                   Body=_format_timestamped(segments).encode(),
                   ContentType="text/plain; charset=utf-8")
    _get_s3().put_object(Bucket=bucket, Key=clean_key,
                   Body=_format_paragraphs(segments).encode(),
                   ContentType="text/plain; charset=utf-8")
    return ts_key, clean_key


# ---------- steps ----------

def _step_download(event):
    job_id, url = event["id"], event["url"]
    _update(job_id, **{"status": "downloading", "started_at": _now()})
    _ensure_pot_server()
    args = argparse.Namespace(
        output_dir="/tmp/downloads", cookies=None, cookies_from_browser="none",
        s3_output=AUDIO_S3, keep_local=True, audio_quality="0",
        pot_server=POT_SERVER, verbose=True,
    )
    dl = AudioDownloader(args)
    try:
        mp3 = dl.download(url)
        if mp3 is None:
            raise RuntimeError(dl.last_error or "download failed")
        if not dl.upload_to_s3(mp3):
            raise RuntimeError("S3 upload failed")
        info = dl.last_info or {}
        s3_key = f"{dl.s3_prefix.rstrip('/')}/{mp3.name}"
        duration = int(info.get("duration") or 0)
        title = info.get("title") or mp3.stem
        _update(job_id, **{"status": "downloaded", "s3_key": s3_key,
                           "title": title, "duration": duration})
        return {"step": "download", "id": job_id, "s3_key": s3_key,
                "duration": duration}
    finally:
        dl.cleanup()
        shutil.rmtree("/tmp/downloads", ignore_errors=True)


def _fetch_audio(s3_key, dest):
    bucket, _ = parse_s3_uri(AUDIO_S3)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    _get_s3().download_file(bucket, s3_key, dest)
    return bucket


def _step_transcribe(event):
    job_id, s3_key = event["id"], event["s3_key"]
    _update(job_id, status="transcribing")
    local = f"/tmp/transcribe/{Path(s3_key).name}"
    _fetch_audio(s3_key, local)
    try:
        segments = _whisper_segments(local)
        ts_key, clean_key = _upload_transcripts(Path(s3_key).stem, segments)
        _update(job_id, **{"status": "done", "transcript_key": ts_key,
                           "transcript_clean_key": clean_key,
                           "finished_at": _now()})
        return {"id": job_id, "transcript_key": ts_key}
    finally:
        shutil.rmtree("/tmp/transcribe", ignore_errors=True)


def _step_chunk(event):
    job_id, s3_key = event["id"], event["s3_key"]
    _update(job_id, status="transcribing")
    local = f"/tmp/chunkwork/{Path(s3_key).name}"
    bucket = _fetch_audio(s3_key, local)
    out_dir = Path("/tmp/chunkwork/chunks")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", local, "-f", "segment",
             "-segment_time", str(CHUNK_SECONDS), "-c", "copy",
             str(out_dir / "%03d.mp3")],
            check=True, capture_output=True,
        )
        chunks = []
        for i, f in enumerate(sorted(out_dir.glob("*.mp3"))):
            key = f"chunks/{job_id}/{f.name}"
            _get_s3().upload_file(str(f), bucket, key,
                            ExtraArgs={"ContentType": "audio/mpeg"})
            chunks.append({"id": job_id, "key": key,
                           "offset_s": i * CHUNK_SECONDS})
        return {"id": job_id, "s3_key": s3_key, "chunks": chunks}
    finally:
        shutil.rmtree("/tmp/chunkwork", ignore_errors=True)


def _step_transcribe_chunk(event):
    job_id, key, offset = event["id"], event["key"], event["offset_s"]
    bucket, _ = parse_s3_uri(AUDIO_S3)
    local = f"/tmp/chunk/{Path(key).name}"
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    _get_s3().download_file(bucket, key, local)
    try:
        segments = _whisper_segments(local, offset_s=float(offset))
        json_key = key.rsplit(".", 1)[0] + ".json"
        _get_s3().put_object(Bucket=bucket, Key=json_key,
                       Body=json.dumps(segments).encode(),
                       ContentType="application/json")
        return {"id": job_id, "json_key": json_key, "offset_s": offset}
    finally:
        shutil.rmtree("/tmp/chunk", ignore_errors=True)


def _step_merge(event):
    job_id, s3_key = event["id"], event["s3_key"]
    results = sorted(event["results"], key=lambda r: r["offset_s"])
    bucket, _ = parse_s3_uri(AUDIO_S3)
    segments = []
    for r in results:
        body = _get_s3().get_object(Bucket=bucket, Key=r["json_key"])["Body"].read()
        segments.extend(json.loads(body))
    ts_key, clean_key = _upload_transcripts(Path(s3_key).stem, segments)
    # Clean up intermediate chunk files
    listing = _get_s3().list_objects_v2(Bucket=bucket, Prefix=f"chunks/{job_id}/")
    keys = [{"Key": o["Key"]} for o in listing.get("Contents", [])]
    if keys:
        _get_s3().delete_objects(Bucket=bucket, Delete={"Objects": keys})
    _update(job_id, **{"status": "done", "transcript_key": ts_key,
                       "transcript_clean_key": clean_key,
                       "finished_at": _now()})
    return {"id": job_id, "transcript_key": ts_key}


_STEPS = {
    "download": _step_download,
    "transcribe": _step_transcribe,
    "chunk": _step_chunk,
    "transcribe_chunk": _step_transcribe_chunk,
    "merge": _step_merge,
}


def handler(event, context):
    step = event.get("step")
    fn = _STEPS.get(step)
    if fn is None:
        raise ValueError(f"Unknown step: {step!r}")
    try:
        return fn(event)
    except (Exception, SystemExit) as e:  # SystemExit: AudioDownloader sys.exit(2)
        job_id = event.get("id")
        if job_id:
            _update(job_id, **{"status": "error", "error": str(e)[:1000],
                               "finished_at": _now()})
        raise RuntimeError(f"step {step} failed: {e}") from e
