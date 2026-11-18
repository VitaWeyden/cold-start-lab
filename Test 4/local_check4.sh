#!/usr/bin/env bash
# Test 4: local calibration of the three sizes before anything is deployed.
# Builds shop-api:v4-s / v4-m / v4-l with Docker, then starts each image REPS times with the
# Cloud Run limits (1 CPU, 2 GiB) and reports: image size, time from `docker run` to the first
# successful /health, the app's start-up phases, model size, peak memory and /api/report work.
# Local numbers are not Cloud Run numbers; they only check that L fits into 2 GiB and that the
# sizes are clearly apart.
#
#   bash "Test 4/local_check4.sh"                    # build + 3 starts per size
#   REPS=5 SIZES="l" MODEL_MB_L=400 bash "Test 4/local_check4.sh"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
REPS="${REPS:-3}"
SIZES="${SIZES:-s m l}"
MODEL_MB_M="${MODEL_MB_M:-20}"
MODEL_MB_L="${MODEL_MB_L:-500}"
PORT="${PORT:-18080}"
PY="${PY:-$(command -v python3 >/dev/null 2>&1 && python3 -c 'print(1)' >/dev/null 2>&1 && echo python3 || echo python)}"
OUT="$HERE/local_check4.csv"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"; docker rm -f coldstart-local4 >/dev/null 2>&1 || true' EXIT
cp "$ROOT/app/data.py" "$ROOT/app/requirements.txt" "$BUILD/"
cp "$HERE/main.py" "$HERE/Dockerfile" "$HERE/requirements-ml.txt" "$HERE/make_model.py" "$BUILD/"
sed -i 's/\r$//' "$BUILD"/*

echo "size,rep,image_mb,run_to_health_ms,startup_ms,imports_ms,init_ms,ml_import_ms,model_load_ms,model_mb,peak_rss_mb,rss_mb,report_work_ms,report_latency_ms" > "$OUT"
for s in $SIZES; do
  echo ">> building shop-api:v4-$s"
  docker build -q --build-arg SIZE="$s" --build-arg MODEL_MB_M="$MODEL_MB_M" \
    --build-arg MODEL_MB_L="$MODEL_MB_L" -t "shop-api:v4-$s" "$BUILD" >/dev/null
  img_mb="$(docker image inspect "shop-api:v4-$s" --format '{{.Size}}' | awk '{printf "%.0f", $1/1048576}')"
  echo "   image: ${img_mb} MB"
  for r in $(seq 1 "$REPS"); do
    docker rm -f coldstart-local4 >/dev/null 2>&1 || true
    t0=$(date +%s%3N)
    docker run -d --name coldstart-local4 --cpus 1 --memory 2g --memory-swap 2g \
      -p "$PORT:8080" "shop-api:v4-$s" >/dev/null
    until curl -fs -o /dev/null "http://localhost:$PORT/health"; do
      if [ "$(docker inspect -f '{{.State.Running}}' coldstart-local4)" != "true" ]; then
        echo "   container stopped:"; docker logs coldstart-local4 | tail -20
        echo "   (OOMKilled: $(docker inspect -f '{{.State.OOMKilled}}' coldstart-local4))"; exit 1
      fi
      sleep 0.05
    done
    t1=$(date +%s%3N)
    body="$(curl -fs -w '\n%{time_total}' "http://localhost:$PORT/api/report")"
    row="$(printf '%s' "$body" | "$PY" -c '
import json, sys
lines = sys.stdin.read().splitlines()
d = json.loads(lines[0]); lat = float(lines[1]) * 1000
keys = ["startup_ms","imports_ms","init_ms","ml_import_ms","model_load_ms","model_mb","peak_rss_mb","rss_mb","work_ms"]
print(",".join(str(d.get(k, "")) for k in keys) + f",{lat:.1f}")')"
    echo "$s,$r,$img_mb,$((t1 - t0)),$row" >> "$OUT"
    echo "   rep $r: run->health $((t1 - t0)) ms | startup,imports,init,ml_import,model_load,model_mb,peak_rss,rss,work,latency = $row"
    docker rm -f coldstart-local4 >/dev/null
  done
done
echo "-> $OUT"
