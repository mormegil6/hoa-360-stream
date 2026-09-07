#!/usr/bin/env bash
# Test for scripts/merge-obs-tracks.sh: proves an OBS-shaped multitrack
# recording merges into a single multichannel stream with CHANNEL ORDER
# preserved, offline always, and through the real pipeline with --e2e.
#
# Stage 1  synthesises two 8-channel tracks tagged 7.1 + H.264 video. That is
#          the SUPERSEDED OBS shape, kept because it still exercises both
#          merge paths; the documented recipe is now four 4-channel tracks
#          tagged 4.0 (docs/obs-macos.md, merge-obs-tracks.sh's header),
#          because a 7.1 layout mutes the LFE slot and so erases ACN 3. Audio is PCM, deliberately: the
#          synthetic exists to test the MERGE, and ffmpeg's AAC encoders are
#          not faithful stand-ins at 8 channels (native aac lowpasses the LFE
#          slot; aac_at negotiates 8ch down to 7 - both measured 2026-07-31).
#          OBS is not on CoreAudio here anyway: the documented recipe picks
#          the plain ffmpeg `aac` encoder and never aac_at, whose 4-channel
#          output reads back scrambled (docs/obs-macos.md). Each channel
#          carries its own tone: 200..1700 Hz, 100 Hz steps, the same ladder
#          as test-pipeline.sh.
# Stage 2  merges to the 16-channel hexadecagonal master and asserts every
#          channel's dominant tone is its own (Goertzel, check-tones.py).
# Stage 3  same through the unnamed-layout path (a 12-channel trim).
# Stage 4  (--e2e, needs the compose stack already running) pushes via the
#          script's --push through rtmp-ingest and earshot, then decodes the
#          emitted DASH Opus segments and runs the same 16-tone assertion on
#          what a viewer would actually receive. Stops loop-source for the
#          duration if it is running, restarts it after.
#
# Usage: ./scripts/test-obs-merge.sh [--e2e]
# Exit codes: 0 PASS, 1 FAIL, 2 precondition error.

set -euo pipefail
cd "$(dirname "$0")/.."

E2E=0
[ "${1:-}" = "--e2e" ] && E2E=1

command -v ffmpeg >/dev/null || { echo "ffmpeg required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 2; }

WORK=scratch/obs-merge-test
rm -rf "$WORK" && mkdir -p "$WORK"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "[1/4] synthesising the OBS-shaped multitrack recording"
IN=()
for i in $(seq 0 15); do
    IN+=(-f lavfi -i "sine=frequency=$((200 + i * 100)):sample_rate=48000:duration=20")
done
ffmpeg -hide_banner -loglevel error -y \
    "${IN[@]}" \
    -f lavfi -i "testsrc2=size=640x320:rate=30:duration=20" \
    -filter_complex "\
[0:a][1:a][2:a][3:a][4:a][5:a][6:a][7:a]amerge=inputs=8,pan=7.1|c0=c0|c1=c1|c2=c2|c3=c3|c4=c4|c5=c5|c6=c6|c7=c7[t1];\
[8:a][9:a][10:a][11:a][12:a][13:a][14:a][15:a]amerge=inputs=8,pan=7.1|c0=c0|c1=c1|c2=c2|c3=c3|c4=c4|c5=c5|c6=c6|c7=c7[t2]" \
    -map 16:v -map "[t1]" -map "[t2]" \
    -c:v libx264 -preset veryfast -g 60 -pix_fmt yuv420p \
    -c:a pcm_s16le \
    "$WORK/obs-shaped.mov" 2>&1
./scripts/merge-obs-tracks.sh --check "$WORK/obs-shaped.mov" \
    | grep -q "2 track(s), channels per track: 8 8 (total 16)" \
    || fail "synthetic input does not have the expected 2x8 shape"

tone_check() { # <file> <channels> [base]
    ffmpeg -v error -ss 5 -i "$1" -map 0:a:0 -t 1.5 -f s16le -c:a pcm_s16le - 2>/dev/null \
        | python3 scripts/check-tones.py "$2" 48000 "${3:-200}" 100
}

echo "[2/4] merge to 16 ch (named hexadecagonal, join path)"
./scripts/merge-obs-tracks.sh "$WORK/obs-shaped.mov" "$WORK/merged16.mov" --channels 16 >/dev/null 2>&1
tone_check "$WORK/merged16.mov" 16 || fail "16-channel merge scrambled the channel order"

echo "[3/4] merge to 12 ch (unnamed layout, strip+amerge path)"
./scripts/merge-obs-tracks.sh "$WORK/obs-shaped.mov" "$WORK/merged12.mov" --channels 12 >/dev/null 2>&1
tone_check "$WORK/merged12.mov" 12 || fail "12-channel merge scrambled the channel order"

if [ "$E2E" -eq 0 ]; then
    echo "PASS (offline stages; rerun with --e2e and the stack up for the pipeline stage)"
    exit 0
fi

echo "[4/4] e2e: push through rtmp-ingest -> earshot, verify the emitted DASH audio"
docker compose ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep -q "earshot.*running" \
    || { echo "the compose stack is not running - start it first (docker compose up -d)" >&2; exit 2; }

env_get() {
    sed -n "s/^$1=//p" .env 2>/dev/null | tail -1 \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
# RTMP_OWNER_KEY, not LOOP_SOURCE_KEY: this pushes under its own stream name,
# and since 2026-08-10 the loop token is scoped to the loop's own name only.
RTMP_OWNER_KEY="${RTMP_OWNER_KEY:-$(env_get RTMP_OWNER_KEY)}"
[ -n "$RTMP_OWNER_KEY" ] || fail "RTMP_OWNER_KEY is not set and .env has none - run ./scripts/setup.sh"
DASH_NAME="${DASH_NAME:-$(env_get DASH_NAME)}";   DASH_NAME="${DASH_NAME:-hoast_demo}"

LOOP_WAS_RUNNING=0
if docker compose ps --format '{{.Name}} {{.State}}' | grep -q "loop-source.*running"; then
    LOOP_WAS_RUNNING=1
    echo "  stopping loop-source for publish exclusivity"
    docker compose stop loop-source >/dev/null 2>&1
    sleep 3
fi
restore() {
    if [ "$LOOP_WAS_RUNNING" -eq 1 ]; then
        docker compose start loop-source >/dev/null 2>&1 || true
    fi
}
trap restore EXIT

./scripts/merge-obs-tracks.sh "$WORK/obs-shaped.mov" \
    --push "rtmp://localhost:1935/owner/obs-merge-test?token=${RTMP_OWNER_KEY}" \
    --channels 16 >/dev/null 2>&1 \
    || fail "push through the ingest failed"

# newest completed audio chunk + its init segment. Stream 1 on purpose, not
# "the audio": earshot writes two audio streams now, and stream 2 is the
# silent stereo keep-alive, which decodes as silence and would be reported as
# a channel-order fault.
sleep 2
# Glob the extension: the container follows the video codec (.m4s under the
# committed -c:v copy + -dash_segment_type mp4 default, .webm under VP9).
INIT=$(find output -maxdepth 1 -name 'init-stream1.*' ! -name '*.tmp' | head -1)
CHUNK=$(find output -maxdepth 1 -name 'chunk-stream1-*' ! -name '*.tmp' | sort | tail -2 | head -1)
[ -f "$INIT" ] && [ -n "$CHUNK" ] || fail "no DASH audio segments appeared in output/"
grep -q 'AudioChannelConfiguration[^/]*value="16"' "output/${DASH_NAME}.mpd" \
    || fail "manifest does not declare 16 audio channels"
# no extension: the container follows FFMPEG_FLAGS (fMP4 under the committed
# -c:v copy default, WebM under the VP9 opt-in), and ffmpeg probes by content
cat "$INIT" "$CHUNK" > "$WORK/dash-audio"
ffmpeg -v error -i "$WORK/dash-audio" -ss 0.2 -t 1.5 -f s16le -c:a pcm_s16le - 2>/dev/null \
    | python3 scripts/check-tones.py 16 \
    || fail "channel order did not survive the pipeline (DASH Opus output)"

echo "PASS (offline + e2e: 16 discrete ordered channels from OBS-shaped input to DASH Opus)"
