# video-to-transcript — audio-only (MP3) downloader for AWS.
#
# Architecture: arch-neutral. Primary target is linux/arm64 (AWS Graviton —
# cheaper, and ffmpeg/Deno/yt-dlp all ship arm64 builds; an Apple Silicon Mac
# builds it natively). For x86_64 instances use:
#   docker buildx build --platform linux/amd64 .
FROM python:3.14-slim

# BGUTIL_TAG must match bgutil-ytdlp-pot-provider in requirements.txt
ARG BGUTIL_TAG=1.3.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: required by yt-dlp's EJS YouTube challenge solver, and also runs the
# bgutil PO-token server (one runtime for both).
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh -s -- --yes

# bgutil PO-token provider server (run from TS source via Deno at startup).
# Pre-cache its npm dependencies at build time so the server starts fast.
# The cache is baked at /opt/deno-cache and made world-readable: on Lambda the
# runtime user is not root and the FS is read-only, so the handler copies it
# to /tmp/deno-cache at cold start (Deno needs a WRITABLE cache dir).
ENV DENO_DIR=/opt/deno-cache
RUN git clone --depth 1 --branch "${BGUTIL_TAG}" \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider /opt/bgutil \
    && cd /opt/bgutil/server \
    && deno install --entrypoint src/main.ts \
    && chmod -R a+rX /opt/deno-cache /opt/bgutil

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Lambda-only deps (not in requirements.txt — the local venv doesn't need them):
# awslambdaric = Lambda Runtime Interface Client for non-AWS base images;
# faster-whisper = transcription engine for the worker pipeline.
RUN pip install --no-cache-dir awslambdaric faster-whisper

# Bake the whisper model into the image so Lambda never downloads it at
# runtime (read-only FS, and cold starts stay fast). Path passed straight to
# WhisperModel() — no HuggingFace cache involvement at runtime.
RUN python -c "from huggingface_hub import snapshot_download; \
snapshot_download('Systran/faster-whisper-base', local_dir='/opt/whisper-models/base')" \
    && chmod -R a+rX /opt/whisper-models
ENV WHISPER_MODEL_PATH=/opt/whisper-models/base

COPY video_to_transcript.py lambda_handler.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV V2T_IN_CONTAINER=1
# /work is the default local output dir; mount a volume here if you want the
# files locally instead of (or in addition to) S3.
WORKDIR /work

ENTRYPOINT ["/app/entrypoint.sh"]
