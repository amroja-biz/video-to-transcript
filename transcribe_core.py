"""Transcription + paragraph-formatting core, shared by both entrypoints.

Pure transcription logic — no AWS, no S3, no DynamoDB. The local CLI
(transcribe_local.py) and the Lambda worker (lambda_handler.py) both import
from here, so a fix to transcription or formatting lands in both modes at once.

Nothing in this module knows or cares where it runs; callers pass in the audio
file and (optionally) a model identifier.
"""

import os

# ---- paragraph formatting tunables ----
PAUSE_BREAK_S = 1.5        # gap between segments that forces a paragraph break
MARKER_MIN_PARA_CHARS = 200   # discourse marker only breaks once para is this long
MAX_PARA_CHARS = 800       # runaway paragraph cap (break at next sentence end)
TOPIC_MARKERS = (
    "so ", "so,", "and then", "now ", "now,", "okay", "anyway",
    "but ", "next ", "alright", "all right",
)

_whisper_model = None  # cached across calls (warm Lambda invokes, or a CLI batch)


def get_model(model=None):
    """Load (and cache) a faster-whisper model.

    Resolution order for the model id: explicit `model` arg > WHISPER_MODEL_PATH
    env var > "base". In Lambda the env var is always set to the baked-in model
    path; locally it's unset, so faster-whisper auto-downloads the "base" model
    on first use.
    """
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        name = model or os.environ.get("WHISPER_MODEL_PATH") or "base"
        _whisper_model = WhisperModel(name, device="cpu", compute_type="int8")
    return _whisper_model


def whisper_segments(path, offset_s=0.0, model=None):
    """Transcribe a file; return [{start, end, text}] with optional time offset."""
    segments, _info = get_model(model).transcribe(str(path), beam_size=5)
    return [
        {"start": float(s.start) + offset_s,
         "end": float(s.end) + offset_s,
         "text": s.text.strip()}
        for s in segments
    ]


def ts(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:05.2f}"


def format_timestamped(segments):
    return "\n".join(f"[{ts(s['start'])} -> {ts(s['end'])}] {s['text']}"
                     for s in segments) + "\n"


def format_paragraphs(segments):
    """Insert paragraph breaks at natural boundaries. Words are never altered —
    only \\n\\n is inserted between whisper segments. Break signals:
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
