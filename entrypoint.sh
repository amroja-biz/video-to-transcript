#!/usr/bin/env bash
# Container entrypoint: start the bgutil PO-token server in the background,
# wait for it to come up, then run the downloader with whatever args were
# passed to `docker run`.
set -euo pipefail

POT_PORT=4416

echo "Starting bgutil PO-token server ..."
(
  cd /opt/bgutil/server
  exec deno run --allow-env --allow-net --allow-ffi=. --allow-read=. src/main.ts
) >/tmp/bgutil.log 2>&1 &

# Wait up to 15s for the server; warn and continue if it never comes up
# (non-YouTube sites work without it).
for i in $(seq 1 30); do
  if (exec 3<>"/dev/tcp/127.0.0.1/${POT_PORT}") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "PO-token server up on :${POT_PORT}"
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "⚠ PO-token server did not start within 15s (see /tmp/bgutil.log)." >&2
    echo "  Continuing — YouTube downloads may fail without it." >&2
  fi
  sleep 0.5
done

exec python /app/audio_downloader.py "$@"
