#!/usr/bin/env bash
# Synthetic end-to-end pipeline test.
#
# Pushes a synthetic contribution stream: 16 sine channels (200..1700 Hz, one
# per channel, hexadecagonal layout) + testsrc2 video, H.264 + 16-ch AAC (PCE)
# in FLV, encoded by the earshot image's PCE-aware ffmpeg, through
# rtmp-ingest's token auth into earshot, then asserts live DASH appears in
# ./output/:
#   - <stream>.mpd manifest: valid XML, 16-ch Opus audio
#     (AudioChannelConfiguration value="16"), the silent stereo keep-alive
#     set (AAC in fMP4, Opus on the WebM opt-in), and the video codec that the
#     EFFECTIVE FFMPEG_FLAGS imply - read from docker compose, never assumed
#   - chunk files within FIRST_SEGMENT_DEADLINE seconds of the push start
#   - at least MIN_CHUNKS chunks per stream (all three) after the push completes
#
# The RTMP contribution leg is H.264 + 16-ch AAC by protocol necessity; the
# earshot transcode (always 16-ch Opus; video per FFMPEG_FLAGS) is what this
# test verifies.
#
# The test publishes as "pipeline-test?token=$RTMP_OWNER_KEY" (exercising the
# token-auth path). earshot writes every stream's chunks into the same
# directory with identical default names, so the test refuses to run while
# another publisher is active; if that publisher is loop-source, it is
# stopped for the duration of the test and restarted afterwards. The stack's
# prior state (running / stopped / absent) is restored on exit.
#
# The DASH manifest is named after DASH_NAME (default hoast_demo), NOT the
# publish name: earshot no longer interpolates the attacker-controllable publish
# name into the output path. This test still publishes as "pipeline-test" (to
# exercise token auth) but asserts on $DASH_NAME.mpd. Its cleanup therefore
# transiently removes the production manifest name, which loop-source
# regenerates within a segment or two of the restart this script performs.
#
# Usage: ./scripts/test-pipeline.sh
# Exit codes: 0 PASS, 1 FAIL, 2 precondition error.

set -euo pipefail
cd "$(dirname "$0")/.."

TEST_STREAM=pipeline-test
PUSH_CONTAINER=ambi-box-pipeline-test-push
PUSH_SECONDS=30
HEALTHY_DEADLINE=120     # first `up` may also build images; polling starts after up returns
FIRST_SEGMENT_DEADLINE=20   # expectation is <15 s from push start
STOP_PUBLISH_DEADLINE=20
MIN_CHUNKS=5             # >=10 s of content at 2 s segments
OUTPUT_DIR=./output

# Read RTMP_OWNER_KEY / FFMPEG_FLAGS the way compose resolves them: shell env
# first, then .env. Never shell-source .env - the compose dialect allows
# unquoted values with spaces (see FFMPEG_FLAGS in .env.example).
env_get() {
    sed -n "s/^$1=//p" .env | tail -1 \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
# This test publishes under a name that is NOT the loop's, on purpose (see the
# TEST_STREAM comment below), and since 2026-08-10 that is exactly what
# LOOP_SOURCE_KEY may no longer do: it is scoped to the loop's own stream name,
# because it was otherwise a general owner-publish credential with a committed
# public default. RTMP_OWNER_KEY is the credential that legitimately publishes
# under any name, which is also what this test is simulating.
if [ -z "${RTMP_OWNER_KEY:-}" ] && [ -f .env ]; then RTMP_OWNER_KEY=$(env_get RTMP_OWNER_KEY); fi
if [ -z "${FFMPEG_FLAGS:-}" ] && [ -f .env ]; then FFMPEG_FLAGS=$(env_get FFMPEG_FLAGS); fi
if [ -z "${RTMP_OWNER_KEY:-}" ]; then
    echo "[test-pipeline] RTMP_OWNER_KEY is not set and .env has none - run ./scripts/setup.sh" >&2
    exit 2
fi
FFMPEG_FLAGS="${FFMPEG_FLAGS:-}"
if [ -z "${DASH_NAME:-}" ] && [ -f .env ]; then DASH_NAME=$(env_get DASH_NAME); fi
DASH_NAME="${DASH_NAME:-hoast_demo}"
# earshot names the manifest after DASH_NAME, not the publish name. TEST_STREAM
# stays the publish name so the ?token= path is still exercised.
TEST_MPD="$OUTPUT_DIR/$DASH_NAME.mpd"
log()  { printf '[test-pipeline] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }
pre()  { log "ERROR: $*"; exit 2; }

command -v docker >/dev/null || pre "docker not found"
docker compose version >/dev/null 2>&1 || pre "docker compose plugin not found"
docker info >/dev/null 2>&1 || pre "docker daemon not running"
mkdir -p "$OUTPUT_DIR"

# Codec policy. An EMPTY FFMPEG_FLAGS does not mean "no policy" - it means the
# compose file's own fallback applies. Read that EFFECTIVE value instead of
# restating it: docker-compose.yml is the single source of truth, and hard-coding
# a guess here is exactly how this script came to assert VP9 while the stack
# shipped passthrough, failing its own test against a correct host. Runs after
# the docker checks above, since it shells out to compose.
# Read it as JSON, not as the YAML rendering: `docker compose config` wraps a
# long scalar onto a continuation line, and this value sits right at the wrap
# column, so a line-oriented read returned "... -c:v" with the "copy" on the
# next line. That silently reclassified passthrough as a VP9 policy and failed
# the run against a perfectly correct host - the same class of false failure
# the comment above already warns about, arriving by a different route.
if [ -z "$FFMPEG_FLAGS" ]; then
    FFMPEG_FLAGS=$(docker compose config --format json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["earshot"]["environment"].get("FFMPEG_FLAGS",""))' 2>/dev/null)
fi
case "$FFMPEG_FLAGS" in
    *"libvpx-vp9"*) VIDEO_CODEC=vp09 ;;   # VP9 transcode
    *"-c:v copy"*)  VIDEO_CODEC=avc1 ;;   # H.264 passthrough
    "")  pre "cannot determine FFMPEG_FLAGS - set it explicitly, or check that 'docker compose config' works" ;;
    *)              VIDEO_CODEC=vp09 ;;   # some other transcode: assume policy
esac
log "codec policy: $VIDEO_CODEC (effective FFMPEG_FLAGS: $FFMPEG_FLAGS)"

# ------------------------------------------ docs-agreement guard ------------
# compose's FFMPEG_FLAGS fallback is canonical; README and .env.example must agree, and
# the shipped stack failed this very script as a result. The compose fallback
# is canonical; everything else must reference it or agree with it. This guard
# checks the CANONICAL DEFAULT (not the deployment's override), so it holds on
# hosts with a custom .env too, and fails the test the moment drift returns.
canonical=$(grep -o 'FFMPEG_FLAGS=\${FFMPEG_FLAGS:--[^}]*}' docker-compose.yml \
    | head -1 | sed 's/^FFMPEG_FLAGS=${FFMPEG_FLAGS:-//; s/}$//')
[ -n "$canonical" ] || pre "docs guard: no FFMPEG_FLAGS fallback found in docker-compose.yml"
case "$canonical" in
    *"-c:v copy"*)   want_readme="H.264 passthrough" ;;
    *"libvpx-vp9"*)  want_readme="VP9" ;;
    *) pre "docs guard: compose fallback has an unrecognised codec: $canonical" ;;
esac
grep -q "currently \*\*${want_readme}\*\*" README.md \
    || pre "docs guard: README's stated default ('currently **...**') does not match the compose fallback ($want_readme)"
grep -q '^FFMPEG_FLAGS=' .env.example \
    && pre "docs guard: .env.example has an ACTIVE FFMPEG_FLAGS line; overrides there must stay commented"
log "docs guard: compose fallback, README and .env.example agree ($want_readme default)"

fetch_stat() { curl -sf --max-time 5 http://localhost:8081/stat 2>/dev/null; }

# ---------------------------------------------------------------- cleanup ---
push_pid=
marker=
restart_loop_source=0
prior_state=absent
cleanup() {
    status=$?
    trap - EXIT
    if [ -n "$push_pid" ] && kill -0 "$push_pid" 2>/dev/null; then
        kill "$push_pid" 2>/dev/null || true
        wait "$push_pid" 2>/dev/null || true
    fi
    # the compose-run client dying does not stop the one-off container
    docker rm -f "$PUSH_CONTAINER" >/dev/null 2>&1 || true
    # let the transcoder notice the publisher is gone before touching output
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        s=$(fetch_stat) || break
        printf '%s' "$s" | grep -q '<publishing/>' || break
        sleep 1
    done
    if [ -n "$marker" ] && [ -f "$marker" ]; then
        # remove only what this run produced
        find "$OUTPUT_DIR" -maxdepth 1 \( -name 'chunk-stream*' -o -name 'init-stream*' \) \
            -newer "$marker" -delete 2>/dev/null || true
        rm -f "$TEST_MPD" "$marker"
    fi
    if [ "$restart_loop_source" = 1 ]; then
        log "restarting loop-source"
        docker compose start loop-source >/dev/null 2>&1 || true
    fi
    case "$prior_state" in
        absent)
            log "stack was not present before the test - taking it down"
            docker compose down >/dev/null 2>&1 || true ;;
        stopped)
            log "stack was stopped before the test - stopping it again"
            docker compose stop >/dev/null 2>&1 || true ;;
    esac
    exit "$status"
}
trap cleanup EXIT

# ------------------------------------------------------------ stack up ------
if [ "$(docker compose ps -q 2>/dev/null | wc -l | tr -d ' ')" -gt 0 ]; then
    prior_state=running
elif [ "$(docker compose ps -aq 2>/dev/null | wc -l | tr -d ' ')" -gt 0 ]; then
    prior_state=stopped
fi
log "docker compose up -d (stack before: $prior_state)"
docker compose up -d

# nginx-rtmp resolves the push target once at startup: if `up` recreated
# earshot but left an older rtmp-ingest running, the relay points at a dead
# IP while both healthchecks stay green. Restart rtmp-ingest in that case.
eid=$(docker compose ps -q earshot 2>/dev/null || true)
iid=$(docker compose ps -q rtmp-ingest 2>/dev/null || true)
if [ -n "$eid" ] && [ -n "$iid" ]; then
    ec=$(docker inspect -f '{{.Created}}' "$eid" 2>/dev/null || true)
    ic=$(docker inspect -f '{{.Created}}' "$iid" 2>/dev/null || true)
    if [ -n "$ec" ] && [ -n "$ic" ] && [[ "$ec" > "$ic" ]]; then
        log "earshot is newer than rtmp-ingest - restarting rtmp-ingest to re-resolve the relay"
        docker compose restart rtmp-ingest >/dev/null
    fi
fi

log "waiting for services to be healthy (up to ${HEALTHY_DEADLINE}s)"
t0=$(date +%s)
while :; do
    unhealthy=$( (docker compose ps --format json 2>/dev/null || true) | python3 -c '
import json, sys
need = {"earshot", "rtmp-ingest", "hoast-player"}
try:
    raw = sys.stdin.read().strip()
    rows = json.loads(raw) if raw.startswith("[") else \
        [json.loads(l) for l in raw.splitlines() if l.strip()]
    healthy = {r.get("Service") for r in rows if r.get("Health") == "healthy"}
except Exception:
    healthy = set()
print(" ".join(sorted(need - healthy)))
')
    [ -z "$unhealthy" ] && break
    [ $(( $(date +%s) - t0 )) -ge "$HEALTHY_DEADLINE" ] && \
        fail "services not healthy after ${HEALTHY_DEADLINE}s: $unhealthy"
    sleep 2
done
log "all services healthy ($(( $(date +%s) - t0 ))s)"

# --------------------------------------------- publisher exclusivity --------
# earshot's chunk filenames collide between concurrent streams, so require
# exclusive use of the transcoder for the duration of the test. A failed stat
# fetch is an error, not "idle" - proceeding blind risks the collision.
stat_xml=$(fetch_stat) || pre "cannot reach earshot's stat endpoint (http://localhost:8081/stat)"

loop_alive=0
if docker compose exec -T loop-source pidof ffmpeg >/dev/null 2>&1; then loop_alive=1; fi

# A live loop-source ffmpeg with NOTHING publishing is not idleness, it is the
# loop's own publish being refused, and it has to be an assertion rather than a
# silent skip.
#
# Since 2026-08-10 this test authenticates with RTMP_OWNER_KEY, because it
# publishes under a name that is deliberately not the manifest name and
# LOOP_SOURCE_KEY is now scoped to the loop's own name. That closed a real hole
# but left the loop's own credential path covered by nothing: a broken DASH_NAME
# hand-off to rtmp-ingest, or an inverted scope check, would leave every
# container healthy, every test green, and the demo loop silently off the air.
# This is that coverage. Polling rather than asserting once, since ffmpeg can be
# alive a second or two before earshot reports the stream.
if [ "$loop_alive" -eq 1 ] && ! printf '%s' "$stat_xml" | grep -q '<publishing/>'; then
    t0=$(date +%s)
    while :; do
        stat_xml=$(fetch_stat) || pre "stat endpoint went away while checking the demo loop"
        printf '%s' "$stat_xml" | grep -q '<publishing/>' && break
        if [ $(( $(date +%s) - t0 )) -ge 20 ]; then
            docker compose logs --tail 20 rtmp-ingest 2>&1 | grep 'rtmp-auth-denied' >&2 || true
            pre "loop-source has a live ffmpeg but nothing is publishing after 20s - its own token/name auth is being refused. Check that DASH_NAME reaches rtmp-ingest and matches what loop-source publishes under"
        fi
        sleep 1
    done
    log "demo loop authenticates: its scoped token and stream name are still accepted"
fi

if printf '%s' "$stat_xml" | grep -q '<publishing/>'; then
    if [ "$loop_alive" -eq 1 ]; then
        log "loop-source is streaming - stopping it for the test"
        restart_loop_source=1   # set BEFORE stop: a failed stop must still restore it
        docker compose stop loop-source >/dev/null
        t0=$(date +%s)
        while :; do
            stat_xml=$(fetch_stat) || pre "stat endpoint went away while draining the stream"
            printf '%s' "$stat_xml" | grep -q '<publishing/>' || break
            [ $(( $(date +%s) - t0 )) -ge "$STOP_PUBLISH_DEADLINE" ] && \
                pre "stream still active ${STOP_PUBLISH_DEADLINE}s after stopping loop-source"
            sleep 1
        done
    else
        pre "another publisher is active (see http://localhost:8081/stat) - stop it and re-run"
    fi
fi

# ------------------------------------------------------------- push ---------
marker=$(mktemp "$OUTPUT_DIR/.test-pipeline.XXXXXX")
rm -f "$TEST_MPD"
docker rm -f "$PUSH_CONTAINER" >/dev/null 2>&1 || true   # stale from a crashed run

# One lavfi graph exposing two output pads: testsrc2 video and a 16-channel
# hexadecagonal bed of sines (200..1700 Hz, one per channel) - a single input
# so one -re paces the whole push in realtime.
graph="testsrc2=size=1920x960:rate=30[out0];"
labels=""
for i in $(seq 0 15); do
    # 2 dB per channel, descending. Frequency proves WHICH channel a tone
    # landed in; the ramp proves it arrived at the right LEVEL, which is the
    # one fault a per-channel gain error produces and frequency cannot see.
    # 2 dB and not 6: sixteen channels at 6 dB apart put the quietest one
    # below check-tones.py's -60 dBFS silence floor and it would report a
    # false SILENT. At 2 dB the span is 30 dB and the last channel clears
    # the floor by ~9 dB, measured.
    graph+="sine=frequency=$((200 + i * 100)):sample_rate=48000,volume=-$((i * 2))dB[s$i];"
    labels+="[s$i]"
done
# join needs an EXPLICIT map, or it does not produce the ladder it looks like it
# produces. Without one, join default-maps by channel NAME: a mono `sine` carries
# layout "mono", which is FC, so input 0 claims FC - slot 2 of
# FL+FR+FC+BL+... - and inputs 1 and 2 then fall into the first unused slots FL
# and FR. The bed arrives as 300,400,200,500... with only the first three
# rotated, which is invisible until something asserts channel order, and then
# looks exactly like a pipeline fault. Measured 2026-08-10.
#
# The production gateway (services/srt-gateway/gateway.py) and
# scripts/merge-obs-tracks.sh have always mapped explicitly, and the latter
# documents why amerge is no better. This harness was the straggler.
#
# Names come from the ffmpeg that will RUN the graph, not the host's, since CI
# has no host ffmpeg and a layout table is not something to hardcode.
ch_names=$(docker compose run --rm --no-deps -T --entrypoint ffmpeg loop-source \
             -hide_banner -layouts 2>/dev/null \
           | awk '$1=="hexadecagonal" {print $2}' | tr '+' ' ')
read -r -a name_arr <<< "$ch_names"
[ "${#name_arr[@]}" -eq 16 ] \
    || pre "hexadecagonal has ${#name_arr[@]} channels in the container's ffmpeg, expected 16"
join_map=""
for i in $(seq 0 15); do
    join_map="${join_map}${join_map:+|}${i}.0-${name_arr[$i]}"
done
graph+="${labels}join=inputs=16:channel_layout=hexadecagonal:map=${join_map}[out1]"

log "pushing ${PUSH_SECONDS}s synthetic H.264 + 16-ch AAC (PCE) to owner/${TEST_STREAM} (token auth)"
push_start=$(date +%s)
docker compose run --rm --no-deps -T --name "$PUSH_CONTAINER" \
    --entrypoint ffmpeg loop-source \
    -hide_banner -loglevel error \
    -re -f lavfi -i "$graph" \
    -map 0:v -map 0:a \
    -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p \
    -b:v 4M -g 60 -keyint_min 60 \
    -c:a aac -b:a 512k -ar 48000 \
    -t "$PUSH_SECONDS" \
    -f flv "rtmp://rtmp-ingest:1935/owner/${TEST_STREAM}?token=${RTMP_OWNER_KEY}" &
push_pid=$!

# ------------------------------------------- first-segment deadline ---------
t_first=
while :; do
    if [ -s "$TEST_MPD" ] && \
       [ -n "$(find "$OUTPUT_DIR" -maxdepth 1 -name 'chunk-stream*' -newer "$marker" -print -quit)" ]; then
        t_first=$(( $(date +%s) - push_start ))
        log "manifest + first chunk after ${t_first}s"

        # ------------------------------------ unauthenticated PLAY must fail ---
        # Publishing to /owner has always needed a token. PLAYING from it needed
        # NOTHING until 2026-08-11, and :1935 is published on all interfaces, so
        # anyone who could reach the port pulled the pre-transcode master - 4K
        # H.264 plus all 16 AAC channels, better than anything the DASH path
        # serves - with no credential and no log line. Verified from a laptop
        # against the reference box before `deny play all;` closed it.
        #
        # Asserted HERE, inside the first-segment block, because that is the one
        # moment this script can prove a publisher is live. A play attempt with
        # nothing publishing fails for the wrong reason, and a test that cannot
        # tell "denied" from "nothing to play" is the exact shape of the three
        # vacuous assertions this suite spent 2026-08-10 removing.
        if docker compose run --rm --no-deps -T --entrypoint ffmpeg loop-source \
                -hide_banner -loglevel error -rw_timeout 10000000 \
                -i "rtmp://rtmp-ingest:1935/owner/${TEST_STREAM}" \
                -t 1 -f null - >/dev/null 2>&1; then
            fail "UNAUTHENTICATED PLAY SUCCEEDED on /owner: the contribution master is readable by anyone who can reach :1935 (expected 'deny play all;' in services/rtmp-ingest/nginx.conf.template)"
        fi
        log "unauthenticated play refused while a publisher was live"
        break
    fi
    kill -0 "$push_pid" 2>/dev/null || fail "push exited before any segment appeared (auth or relay problem?)"
    [ $(( $(date +%s) - push_start )) -ge "$FIRST_SEGMENT_DEADLINE" ] && \
        fail "no manifest+chunk within ${FIRST_SEGMENT_DEADLINE}s of push start"
    sleep 1
done

push_rc=0
wait "$push_pid" || push_rc=$?
push_pid=
[ "$push_rc" -eq 0 ] || fail "synthetic push exited with status $push_rc"

# ------------------------------------------------------------ asserts -------
mpd="$TEST_MPD"
[ -s "$mpd" ] || fail "manifest $mpd missing after push"

if command -v xmllint >/dev/null; then
    xmllint --noout "$mpd" || fail "manifest is not valid XML"
else
    python3 -c "import xml.etree.ElementTree as ET, sys; ET.parse(sys.argv[1])" "$mpd" \
        || fail "manifest is not valid XML"
fi

grep -q "$VIDEO_CODEC" "$mpd" || fail "manifest lacks expected video codec $VIDEO_CODEC"
# Scoped to the AdaptationSet that also declares 16 channels: on the WebM
# opt-in the keep-alive set is Opus too, so a bare grep for codecs="opus" is
# satisfied by the silent stereo track even with the programme encode broken -
# which is what the `opus` entry in scripts/verify-tests-can-fail.sh mutates.
python3 - "$mpd" <<'PY' || fail "manifest lacks Opus audio"
import re, sys
blocks = open(sys.argv[1]).read().split('<AdaptationSet')[1:]
sys.exit(0 if any('codecs="opus"' in b and
                  re.search(r'AudioChannelConfiguration[^>]*value="16"', b)
                  for b in blocks) else 1)
PY
grep -q 'AudioChannelConfiguration' "$mpd" || fail "manifest lacks AudioChannelConfiguration"
grep -q 'value="16"' "$mpd" || fail "manifest does not advertise 16 audio channels"

# ------------------------------------------------ keep-alive set ------------
# The third AdaptationSet: silent stereo audio at 8 kb/s, there so WebKit does
# not suspend a backgrounded player. Safari drops the 16-channel Opus set as
# undecodable, which left the <video> element with no audio track at all, and
# audio died about 2 s after the viewer switched Space or tab; the exec-line
# comment in services/earshot/src/nginx-transcoder/nginx-no-ssl.conf carries
# the measurements. The codec follows the container, decided by earshot's
# entrypoint from the same FFMPEG_FLAGS this script read above: AAC in fMP4,
# Opus everywhere else, because dashenc's per-stream "auto" typing would put an
# AAC keep-alive in fMP4 beside a WebM video and Opus programme and split the
# manifest across two containers (measured). MIRRORS the rule in earshot's
# nginx-transcoder/entrypoint.sh, which is the source of truth: change both
# together or this asserts a codec the encoder no longer emits. Asserted per
# AdaptationSet block rather than by a bare grep for value="2", so an attribute
# sitting in the wrong set cannot satisfy it.
case " $FFMPEG_FLAGS " in
    *" -dash_segment_type mp4 "*) KEEPALIVE_CODEC=mp4a.40.2 ;;
    *)                            KEEPALIVE_CODEC=opus ;;
esac
python3 - "$mpd" "$KEEPALIVE_CODEC" <<'PY' || fail "manifest lacks the silent stereo keep-alive set (no AdaptationSet with codecs=\"$KEEPALIVE_CODEC\" and AudioChannelConfiguration value=\"2\")"
import re, sys
text = open(sys.argv[1]).read()
blocks = text.split('<AdaptationSet')[1:]
ok = any('codecs="%s"' % sys.argv[2] in b
         and re.search(r'AudioChannelConfiguration[^>]*value="2"', b) for b in blocks)
sys.exit(0 if ok else 1)
PY
log "keep-alive set present: codecs=\"$KEEPALIVE_CODEC\", 2 channels"

# Stream indices follow the exec lines' -map order: 0 video, 1 the 16-ch Opus
# programme, 2 the keep-alive.
chunks_v=$(find "$OUTPUT_DIR" -maxdepth 1 -name 'chunk-stream0-*' -newer "$marker" | wc -l | tr -d ' ')
chunks_a=$(find "$OUTPUT_DIR" -maxdepth 1 -name 'chunk-stream1-*' -newer "$marker" | wc -l | tr -d ' ')
chunks_k=$(find "$OUTPUT_DIR" -maxdepth 1 -name 'chunk-stream2-*' -newer "$marker" | wc -l | tr -d ' ')
log "chunks written: stream0=$chunks_v stream1=$chunks_a stream2=$chunks_k"
[ "$chunks_v" -ge "$MIN_CHUNKS" ] && [ "$chunks_a" -ge "$MIN_CHUNKS" ] && [ "$chunks_k" -ge "$MIN_CHUNKS" ] || \
    fail "expected at least $MIN_CHUNKS chunks per stream (got $chunks_v/$chunks_a/$chunks_k)"

# ------------------------------------------------ channel order -------------
# This test has pushed a 200..1700 Hz ladder, one tone per channel, since it was
# written, and until 2026-08-10 nothing ever looked at where those tones came
# out. Everything above is a COUNT: a chain that silently permuted channels
# satisfies all of it. Order is the property the whole exercise exists to
# protect - it is what ACN/SN3D means in practice - and the SRT harness has
# asserted it while this one, which runs on every PR, did not.
# The audio init segment's EXTENSION follows the container, which follows the
# video codec: .webm on the VP9 path, .m4s under -dash_segment_type mp4 (the
# H.264 passthrough default). Glob for it rather than naming one, so this check
# does not silently stop finding it the next time the container changes - which
# is exactly what it did when the passthrough path moved to fMP4.
# Stream 1 on purpose, not "the audio": stream 2 is the silent keep-alive, and
# decoding that here would report sixteen silent channels as an order fault.
init_a=$(find "$OUTPUT_DIR" -maxdepth 1 -name 'init-stream1.*' ! -name '*.tmp' | head -1)
chunk_a=$(find "$OUTPUT_DIR" -maxdepth 1 -name 'chunk-stream1-*' ! -name '*.tmp' -newer "$marker" \
          | sort | tail -2 | head -1)
if [ ! -f "$init_a" ] || [ -z "$chunk_a" ]; then
    fail "cannot check channel order: no init segment or no fresh audio chunk"
fi
cat "$init_a" "$chunk_a" > "$OUTPUT_DIR/.tone.seg"
# The earshot image's ffmpeg, not the host's: CI has none, and this is the same
# fallback test-srt-ingest.sh and make-demo-loop.sh use.
docker run --rm -v "$PWD/$OUTPUT_DIR:/o" --entrypoint ffmpeg ambi-box-earshot:local \
    -v error -i /o/.tone.seg -ss 0.5 -t 1.5 -f s16le -c:a pcm_s16le - \
    > "$OUTPUT_DIR/.tone.pcm" 2>"$OUTPUT_DIR/.tone.err" || true
if [ ! -s "$OUTPUT_DIR/.tone.pcm" ]; then
    head -3 "$OUTPUT_DIR/.tone.err" >&2 || true
    rm -f "$OUTPUT_DIR/.tone.seg" "$OUTPUT_DIR/.tone.pcm" "$OUTPUT_DIR/.tone.err"
    fail "could not decode the DASH audio to check channel order"
fi
set +e
python3 scripts/check-tones.py 16 48000 200 100 2 < "$OUTPUT_DIR/.tone.pcm"
tone_rc=$?
set -e
rm -f "$OUTPUT_DIR/.tone.seg" "$OUTPUT_DIR/.tone.pcm" "$OUTPUT_DIR/.tone.err"
case "$tone_rc" in
    0) log "channel order survived the RTMP path: all 16 tones in their own channels" ;;
    2) fail "decoded the WRONG STREAM, not an order fault: the audio checked was not this test's ladder" ;;
    *) fail "channel order did NOT survive the RTMP path (see the per-channel report above)" ;;
esac

# ---------------------------------- the MASTER's colour range ----------------
# Checked separately from the delivered segments above, because this test pushes
# its own synthetic video: the delivered bytes during a run are testsrc2, not
# what viewers actually receive. The master is what ships.
#
# Measured 2026-08-11: the reference box's demo master is yuvj420p(pc) - FULL
# range - and passes through untouched under -c:v copy. That plays correctly
# everywhere today, including a Quest 3, because it is H.264. It becomes the
# known-broken combination the moment FFMPEG_FLAGS is switched to the documented
# VP9 policy, which is a one-line change in .env. The rule is documented ("any
# re-encoded master must convert"); nothing checked it.
if [ -f content/demo.mp4 ]; then
    minfo=$( { docker run --rm -v "$PWD/content:/c:ro" --entrypoint ffmpeg ambi-box-earshot:local \
                 -hide_banner -i /c/demo.mp4 2>&1 | grep -m1 'Video:'; } || true )
    case "$minfo" in
        *"(pc,"*|*"(full,"*)
            if [ "$VIDEO_CODEC" = "vp09" ]; then
                fail "content/demo.mp4 is FULL RANGE and the codec policy is VP9: this is the combination that breaks dash.js on real GPU browsers while passing headless. Convert it: -vf scale=in_range=pc:out_range=tv -color_range tv"
            fi
            log "NOTE: content/demo.mp4 is full range (pc). Harmless under H.264 passthrough, but switching FFMPEG_FLAGS to VP9 would produce the broken combination - convert the master first" ;;
        *) : ;;
    esac
fi

# ---------------------------------- the MASTER's GOP, under passthrough ------
# Under -c:v copy earshot never re-encodes video, so its -g never applies and the
# DASH muxer can only close a video segment on a keyframe the CONTRIBUTION
# encoder already placed. Segment duration is therefore whatever the master's GOP
# is, and -seg_duration becomes a floor rather than the value it looks like.
#
# Measured 2026-08-12: a master re-encoded for colour range was written without
# -g, so x264 used its default 250-frame keyint and the live path served 8.342 s
# video segments against 2 s audio - quadrupling live latency and coarsening DVR
# seeking, while every document still said 2 s. The master it replaced had 2.002 s
# keyframes, so this was a regression, and nothing in the suite could see it.
#
# Only meaningful under passthrough: with the VP9 policy earshot re-encodes and
# its own -g governs, whatever the master looks like.
if [ -f content/demo.mp4 ] && [ "$VIDEO_CODEC" != "vp09" ]; then
    seg_target=$(printf '%s\n' "$FFMPEG_FLAGS" \
                 | grep -oE '\-seg_duration[[:space:]]+[0-9.]+' | awk '{print $2}' | head -1)
    seg_target=${seg_target:-2}
    # ffprobe is not in the earshot image; ffmpeg's showinfo gives the same thing.
    # Take the LARGEST gap in the window: a duplicate keyframe at t=0 or an extra
    # one at a scene cut can only make a gap smaller, never larger than the GOP.
    gop=$( { docker run --rm -v "$PWD/content:/c:ro" --entrypoint ffmpeg ambi-box-earshot:local \
               -hide_banner -v info -t 40 -i /c/demo.mp4 \
               -vf "select=eq(pict_type\,I),showinfo" -an -f null - 2>&1 \
             | grep -oE 'pts_time:[0-9.]+' | cut -d: -f2 \
             | awk 'NR>1{d=$1-p; if(d>m) m=d} {p=$1} END{printf "%.3f", m+0}'; } || true )
    if [ -n "$gop" ] && awk -v g="$gop" 'BEGIN{exit !(g>0)}'; then
        if awk -v g="$gop" -v s="$seg_target" 'BEGIN{exit !(g > s*1.10)}'; then
            fail "content/demo.mp4 has a ${gop}s GOP but -seg_duration is ${seg_target}s, and the codec policy is passthrough. earshot cannot close a segment between the master's keyframes, so delivered video segments will be ${gop}s, not ${seg_target}s. Re-encode the master with -g \$(2 x fps) -keyint_min the same -sc_threshold 0"
        else
            log "master GOP ${gop}s fits the ${seg_target}s segment target"
        fi
    fi
fi

# ---------------------------------- delivered video colour range -------------
# The one bug that escaped this project into production was full-range (pc) VP9:
# it broke the dash.js/MSE player with PIPELINE_ERROR_DECODE on real GPU
# browsers, decoded fine in plain single-file playback, and PASSED the headless
# harness. The browser check cannot catch that class, but the property whose
# violation caused it can be asserted on the bytes a viewer receives.
#
# SCOPED TO VP9 ON PURPOSE. Measured 2026-08-11: the reference box serves
# full-range H.264 today, inherited from a demo master that is yuvj420p(pc) and
# passed through untouched by -c:v copy, and it plays correctly everywhere
# including a Quest 3. Full-range H.264 is not the hazard; full-range VP9 is.
# So this fails only for the combination that is known to break, and warns for
# the one that is known to work - otherwise it would fail a healthy deployment,
# which is how a real assertion gets deleted for crying wolf.
#
# The LATENT hazard is the point: switching FFMPEG_FLAGS to the documented VP9
# policy, with a master like this one, produces exactly the broken combination.
# ffmpeg, not ffprobe: the earshot image ships only ffmpeg.
init_v=$(find "$OUTPUT_DIR" -maxdepth 1 -name 'init-stream0.*' -print -quit 2>/dev/null)
chunk_v=$(find "$OUTPUT_DIR" -maxdepth 1 -name 'chunk-stream0-*' ! -name '*.tmp' -newer "$marker" \
             -print 2>/dev/null | sort | tail -1)
if [ -n "$init_v" ] && [ -f "$init_v" ] && [ -n "$chunk_v" ]; then
    cat "$init_v" "$chunk_v" > "$OUTPUT_DIR/.range.seg"
    # `|| true`: this script runs under `set -euo pipefail`, so a grep that
    # matches nothing returns 1 and kills the run with no message, before the
    # PASS line. That is the third time today the same construct did it.
    vinfo=$( { docker run --rm -v "$PWD/$OUTPUT_DIR:/o" --entrypoint ffmpeg ambi-box-earshot:local \
                -hide_banner -i /o/.range.seg 2>&1 | grep -m1 'Video:'; } || true )
    rm -f "$OUTPUT_DIR/.range.seg"
    case "$vinfo" in
        *"(pc,"*|*"(full,"*)
            if [ "$VIDEO_CODEC" = "vp09" ]; then
                fail "delivered video is FULL-RANGE VP9, the combination that breaks the dash.js/MSE player on real GPU browsers while passing headless (lip-sync-test/RESULTS.md). Convert the master: -vf scale=in_range=pc:out_range=tv -color_range tv"
            fi
            log "NOTE: delivered video is full range, but H.264, which browsers handle. Switching FFMPEG_FLAGS to VP9 with this source WOULD produce the broken combination - convert the master first"
            ;;
        *"(tv,"*|*"(limited,"*) log "delivered video is limited range" ;;
        "") log "could not read the delivered video's range; not asserting" ;;
        *)  log "delivered video range not stated by ffmpeg; not asserting" ;;
    esac
fi

log "PASS: first segment after ${t_first}s (deadline ${FIRST_SEGMENT_DEADLINE}s), $chunks_v+$chunks_a+$chunks_k chunks, 16-ch Opus + $VIDEO_CODEC + $KEEPALIVE_CODEC keep-alive manifest OK, channel order verified"
