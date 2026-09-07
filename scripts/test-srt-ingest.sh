#!/usr/bin/env bash
# E2E test for the SRT guest gateway: proves a live OBS-shaped SRT push (one
# mpegts, H.264 + four 4-channel AAC tracks) is admitted by the arbiter,
# merged to 16 discrete channels, and emerges from the full pipeline as
# 16-ch DASH Opus with every channel in its slot - and that a second
# concurrent caller is refused at the SRT handshake itself. The session pushes
# a HOSTILE streamid throughout, so the sanitiser is asserted on a stream that
# is actually live rather than on a refused one.
#
# Needs the compose stack already running with GUEST_ENABLED=1 and
# SRT_ENABLED=1 (see .env.example).
#
# WHICH ROUTE THIS EXERCISES IS NOT FIXED, and the header used to claim it was.
# It runs whatever GUEST_SRT_DIRECT says and now prints which. .env.example
# ships 0, the RTMP republish hop; the reference box runs 1, direct to earshot.
# So on that box this suite had been exercising the direct path while its own
# header asserted the other one. That difference matters for more than accuracy:
# only the republish route puts the guest's name into a URL, so only there is
# there a sanitiser to assert (see the gateway-sanitiser block below).
#
# GUEST_GW_SECRET is optional on the republish route, where telemetry honours
# the gateway's ?realip= attribution without one. It is NOT optional for the
# direct path, where an unauthenticated gateway refuses guests.
#
# The SRT caller runs inside the compose network using the gateway's own image,
# so the host needs no libsrt-enabled ffmpeg.
#
# Ends the session with the dashboard kill, which leaves the standard
# operator-kill cooldown (GUEST_COOLDOWN_S, default 300 s) on the guest slot.
#
# Usage: ./scripts/test-srt-ingest.sh
# Exit codes: 0 PASS, 1 FAIL, 2 precondition error.

set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${COMPOSE_PROJECT_NAME:-ambi-box}"
NET="${PROJECT}_default"
TEL=http://127.0.0.1:8090
WORK=scratch/srt-e2e
CALLER=srt-e2e-caller

fail() { echo "FAIL: $*" >&2; exit 1; }

SEGMARK=
cleanup() {
    docker rm -f "$CALLER" >/dev/null 2>&1 || true
    # output/ is the live DASH directory, so leave nothing of ours in it
    [ -n "$SEGMARK" ] && rm -f "$SEGMARK"
    return 0
}
trap cleanup EXIT

echo "[1/6] preconditions"
# Clip synthesis needs an ffmpeg, but not necessarily one on the host. Demanding
# a host binary made the RECOMMENDED route's test the only one a Docker-only
# machine could not run, while scripts/make-demo-loop.sh had already solved the
# same problem by borrowing the earshot image's ffmpeg. Prefer the host one when
# it is there (no container start, no bind mount), fall back to the image when
# it is not. Found by CI: ubuntu-latest no longer ships ffmpeg.
if command -v ffmpeg >/dev/null 2>&1; then
    FFMPEG_MODE=host
elif docker image inspect ambi-box-earshot:local >/dev/null 2>&1; then
    FFMPEG_MODE=container
else
    echo "need an ffmpeg: install one, or build the image (docker compose build earshot)" >&2
    exit 2
fi

# ONE dispatcher for every ffmpeg call in this script, and $FFW for the working
# directory as that ffmpeg sees it. There are two such calls, synthesis and
# decode; fixing only the first cost a CI cycle, because the decode's
# `2>/dev/null` swallowed "command not found" and the empty pipe reached
# check-tones.py as "0 samples", which the script then reported as a channel
# ORDER failure. A wrong diagnosis is worse than a loud one. Route any new
# ffmpeg call through here.
if [ "$FFMPEG_MODE" = host ]; then
    FFW="$WORK"
    ff() { ffmpeg "$@"; }
else
    FFW=/w
    ff() { docker run --rm -v "$PWD/$WORK:/w" --entrypoint ffmpeg ambi-box-earshot:local "$@"; }
fi
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 2; }
docker compose ps --format '{{.Service}} {{.State}}' | grep -q "earshot running" \
    || { echo "compose stack not running (docker compose up -d)" >&2; exit 2; }
docker compose ps --format '{{.Service}} {{.State}}' | grep -q "srt-gateway running" \
    || { echo "srt-gateway not running" >&2; exit 2; }
GWSTATUS=$(docker compose exec -T srt-gateway python3 -c \
    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8091/status', timeout=3).read().decode())")
echo "  gateway: $GWSTATUS"
echo "$GWSTATUS" | grep -q '"enabled": true' \
    || { echo "gateway idle - set SRT_ENABLED=1 and recreate (GUEST_GW_SECRET is optional)" >&2; exit 2; }
curl -sf --max-time 3 "$TEL/api/live" >/dev/null \
    || { echo "telemetry not reachable on $TEL" >&2; exit 2; }

echo "[2/6] synthesising the OBS-shaped SRT payload (H.264 + 4x quad AAC tone ladder)"
rm -rf "$WORK" && mkdir -p "$WORK"
IN=()
for i in $(seq 0 15); do
    IN+=(-f lavfi -i "sine=frequency=$((100 + i * 100)):sample_rate=48000:duration=20")
done
FC=""
MAPS=()
for t in 0 1 2 3; do
    a=$((t * 4 + 1)); b=$((t * 4 + 2)); c=$((t * 4 + 3)); d=$((t * 4 + 4))
    # 4.0, not quad: it is what OBS tags its tracks, and it is the layout
    # proven to round-trip ffmpeg's AAC positionally (quad measurably
    # scrambles channel 2 through encode/decode - caught by this harness)
    FC="${FC}[${a}:a][${b}:a][${c}:a][${d}:a]amerge=inputs=4,pan=4.0|c0=c0|c1=c1|c2=c2|c3=c3[t${t}];"
    MAPS+=(-map "[t${t}]")
done
FF_ARGS=(-hide_banner -loglevel error -y
    -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=20"
    "${IN[@]}"
    -filter_complex "${FC%;}"
    -map 0:v "${MAPS[@]}"
    -c:v libx264 -preset veryfast -g 60 -pix_fmt yuv420p
    -c:a aac -b:a 384k
    -f mpegts)

[ "$FFMPEG_MODE" = host ] || echo "  (no host ffmpeg; using the earshot image)"
ff "${FF_ARGS[@]}" "$FFW/clip.ts"
[ -s "$WORK/clip.ts" ] || { echo "clip synthesis produced nothing" >&2; exit 2; }

# The streamid is the ONE value an SRT caller fully controls. Unlike an RTMP
# publish name, an SRT client will actually SEND this: verified 2026-08-11
# against the gateway image's own ffmpeg, a caller with this streamid reaches
# the connection stage exactly as a legal one does, where the equivalent RTMP
# name is refused before a byte leaves the client.
#
# NO `&`, AND THAT IS A MEASUREMENT, NOT A STYLE CHOICE. This string used to end
# `&x=1 ../../etc/passwd`, and the gateway logged the streamid arriving cut off
# at the ampersand: ffmpeg parses it as the next URL option, so everything after
# it became separate options and never reached the wire. A test asserting on
# bytes that are silently dropped in the client is testing the client. What
# remains is everything an SRT caller can really transmit here - angle brackets,
# both quote kinds, a semicolon, a backtick, a dollar-substitution and an SQL
# comment - and it is what the gateway was observed to receive intact.
# The A-run is not padding. Without it this string sanitises to 31 characters,
# one under the 32 cap, so the length assertion below could never fail no matter
# what the sanitiser did - the same vacuous shape as the three assertions removed
# on 2026-08-10, found on 2026-08-11 by scripts/verify-tests-can-fail.sh. A's are
# used because they are URL-safe and provably transmittable, so lengthening the
# input cannot break the republish and mask the very assertion it enables.
HOSTILE_SID='<script>alert(1)</script>";DROP TABLE--`$(id)`'"$(printf 'A%.0s' $(seq 1 40))"

echo "[3/6] pushing as an SRT caller from inside the compose network"
# Marker written BEFORE the push, so step 5 can tell this session's segments
# from whatever was already in output/. The demo loop is normally publishing
# when this test starts, and its chunks sit in the same directory with the same
# names - so "the two newest chunks" is only the right answer once the SRT
# session has actually written some. Same idiom as test-pipeline.sh.
SEGMARK=$(mktemp output/.srt-e2e.XXXXXX)
# -map 0 is load-bearing: without it ffmpeg's default stream selection sends
# ONE audio track of the four (the same footgun the Windows SRT receiver test
# hit), and the gateway's fixed 4x4 join then correctly refuses the input
docker run -d --name "$CALLER" --network "$NET" \
    -v "$PWD/$WORK:/w:ro" --entrypoint ffmpeg ambi-box-srt-gateway:local \
    -hide_banner -loglevel warning -re -stream_loop -1 -i /w/clip.ts \
    -map 0 -c copy -f mpegts \
    "srt://srt-gateway:8890?mode=caller&streamid=$HOSTILE_SID&latency=2000000" >/dev/null

# This session's streamid is HOSTILE on purpose (defined above), so the adopted
# name is DISCOVERED from the arbiter rather than hardcoded: what matters is
# that whatever survives sanitising is inside the allowlist and under the cap,
# not that it equals a string this script guessed.
ADOPTED=0
for _ in $(seq 1 30); do
    sleep 2
    STATE=$(curl -s --max-time 3 "$TEL/api/live" || true)
    SIDNAME=$(printf '%s' "$STATE" | sed -n 's/.*"name": "\([^"]*\)".*/\1/p')
    if [ -n "$SIDNAME" ] && echo "$STATE" | grep -q '"state": "live"'; then
        ADOPTED=1; break
    fi
    docker ps -q --no-trunc --filter name="$CALLER" | grep -q . \
        || fail "caller exited before adoption (rejected? check srt-gateway logs)"
done
[ "$ADOPTED" -eq 1 ] || fail "session never adopted (state: $(curl -s $TEL/api/live))"

# The hostile-streamid assertions, made against a session that is actually LIVE.
# They used to be a step [7/7] that ran AFTER the operator kill, where the
# cooldown refused the caller by construction. gateway.py resolves a name only
# on the ACCEPT branch (`name = sanitize(...)` inside `if not reason`) and logs
# `reject {ip} ({reason})` with no streamid otherwise, so no accept line ever
# appeared for that push; the grep then matched THIS step's own line from
# earlier in the run and every assertion passed against the benign name. Both
# green CI runs on 2026-08-10 printed `gateway resolved it to name='srte2e'`,
# which was the tell. Asserting here costs no extra session, cannot match a
# stale line, and proves the sanitised name survives a WORKING session.
case "$SIDNAME" in
    *[!A-Za-z0-9_-]*) fail "streamid sanitiser passed characters outside the allowlist: '$SIDNAME'" ;;
esac
[ "${#SIDNAME}" -le 32 ] || fail "sanitised streamid is ${#SIDNAME} chars, cap is 32"
[ "$SIDNAME" != "$HOSTILE_SID" ] || fail "the raw hostile streamid reached the arbiter unsanitised"
echo "  adopted: hostile streamid sanitised to '$SIDNAME' and live"

# THE GATEWAY'S OWN SANITISER, asserted where telemetry cannot mask it.
#
# Everything above reads /api/live, which is TELEMETRY's view, and telemetry
# sanitises independently (collect.py _guest_sanitize). So those assertions
# prove the invariant holds end to end without proving which layer enforces it -
# demonstrated on 2026-08-11, when neutering the gateway's cap alone left the
# suite green because telemetry's cap still applied.
#
# This one reads the name from RTMP-INGEST's stat page. Telemetry is not in that
# path: the gateway interpolates its own sanitised name straight into
#     rtmp://rtmp-ingest:1935/guest/{name}?realip=...&gw={GW_SECRET}
# so the gateway's sanitize() is the ONLY thing standing between a caller's
# streamid and an internal URL carrying the shared secret. A '?' or '&' surviving
# into that name is query injection against an authenticated endpoint, and until
# now nothing asserted it.
# Which route is live decides where the name is observable, and the two differ
# in whether the risk exists at all. On the REPUBLISH route the gateway builds
#     rtmp://rtmp-ingest:1935/guest/{name}?realip=...&gw={GW_SECRET}
# so an unsanitised name is query injection against an authenticated endpoint.
# On the DIRECT route (gateway.py:248) it dials tcp://earshot:<port> and the name
# is never used, so there is nothing to inject into. .env.example ships
# GUEST_SRT_DIRECT=0, so the DEFAULT deployment is the one carrying the risk.
# Read from the RUNNING gateway, not from the compose file or .env. The
# operator-facing knob is GUEST_SRT_DIRECT, but docker-compose.yml:503 maps it to
# SRT_DIRECT inside the service, so `docker compose config | grep
# GUEST_SRT_DIRECT` matches nothing and a first attempt silently took the wrong
# branch. The container's own env is the only place that cannot disagree with
# what is actually running.
GUEST_DIRECT=$(docker inspect "${PROJECT}-srt-gateway-1" \
    --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep -m1 '^SRT_DIRECT=' | cut -d= -f2 | tr -d '[:space:]')
GUEST_DIRECT=${GUEST_DIRECT:-0}

if [ "$GUEST_DIRECT" = "1" ]; then
    echo "  gateway sanitiser: NOT asserted - this stack runs GUEST_SRT_DIRECT=1, where"
    echo "    the gateway dials earshot by address and never puts the name in a URL."
    echo "    The republish route, which .env.example ships as the default, is the one"
    echo "    with the injection surface; run this suite with GUEST_SRT_DIRECT=0 to cover it."
else
    ING_NAME=$(docker compose exec -T telemetry sh -c \
        'curl -s --max-time 4 http://rtmp-ingest:8080/stat' 2>/dev/null | python3 -c '
import sys, re
x = sys.stdin.read()
app = re.search(r"<name>guest</name>(.*?)</application>", x, re.S)
if app:
    names = re.findall(r"<stream>.*?<name>([^<]*)</name>.*?<publishing/>", app.group(1), re.S)
    print(names[0] if names else "")
' 2>/dev/null)
    if [ -z "$ING_NAME" ]; then
        fail "could not read the publishing stream name from rtmp-ingest; the gateway sanitiser assertion cannot be made"
    else
        case "$ING_NAME" in
            *[!A-Za-z0-9_-]*) fail "the GATEWAY sanitiser let characters outside the allowlist into the republish URL: '$ING_NAME'" ;;
        esac
        [ "${#ING_NAME}" -le 32 ] || fail "the GATEWAY sanitiser let a ${#ING_NAME}-char name into the republish URL, cap is 32"
        echo "  gateway's own sanitiser: rtmp-ingest sees '$ING_NAME' (telemetry not in this path)"
    fi
fi

echo "[4/6] verifying 16-ch DASH output (manifest + per-channel tones)"
# Poll rather than sleep a flat 10 s. Segments are 2 s, so two of them is the
# real precondition, and a slow or loaded host simply needs longer to cut them.
# A fixed sleep turns that into an intermittent red for a stack that is working.
SEGDEADLINE=60
t0=$(date +%s)
while :; do
    # Glob the extension: the container follows the video codec (.m4s under the
    # committed -c:v copy + -dash_segment_type mp4 default, .webm under VP9).
    # Stream 1 on purpose, not "the audio": earshot writes two audio streams
    # now and stream 2 is the silent keep-alive, which check-tones.py would
    # report as a channel-order fault.
    INIT=$(find output -maxdepth 1 -name 'init-stream1.*' ! -name '*.tmp' | head -1)
    # -newer "$SEGMARK": only chunks this session produced. Without it the poll
    # matches the demo loop's leftovers on its first iteration and the whole
    # tone check runs against the wrong stream, reporting a channel-ORDER fault
    # for a perfectly good SRT session. That is what CI did on 2026-08-10, and
    # the giveaway in the log was "segments after 0s".
    FRESH=$(find output -maxdepth 1 -name 'chunk-stream1-*' -newer "$SEGMARK" 2>/dev/null | wc -l | tr -d ' ')
    if [ -f "$INIT" ] && [ "$FRESH" -ge 2 ]; then
        CHUNK=$(find output -maxdepth 1 -name 'chunk-stream1-*' -newer "$SEGMARK" 2>/dev/null \
                | sort | tail -2 | head -1)
        break
    fi
    if [ $(( $(date +%s) - t0 )) -ge "$SEGDEADLINE" ]; then
        fail "only $FRESH new DASH segments after ${SEGDEADLINE}s (session was adopted, so earshot is not cutting for it)"
    fi
    sleep 2
done
echo "  $FRESH fresh segments after $(( $(date +%s) - t0 ))s"

MPD=$(ls output/*.mpd 2>/dev/null | head -1)
[ -n "$MPD" ] || fail "no MPD in output/"
grep -q 'AudioChannelConfiguration[^/]*value="16"' "$MPD" \
    || fail "manifest does not declare 16 audio channels"
# Neutral extension: the segment container follows the video codec policy, so
# this is fMP4 under the committed -c:v copy default and WebM under the VP9
# opt-in. ffmpeg probes the content either way, so do not pin it back to .webm.
cat "$INIT" "$CHUNK" > "$WORK/dash-audio.seg"

# Decode to a file first, and assert it is non-empty, so a decode failure says
# so instead of arriving at check-tones.py as an empty stream and being reported
# as a channel-ORDER fault. That misdiagnosis is exactly what happened on
# 2026-08-10 when this call still required a host ffmpeg.
ff -v error -i "$FFW/dash-audio.seg" -ss 0.2 -t 1.5 -f s16le -c:a pcm_s16le - \
    > "$WORK/dash.pcm" 2> "$WORK/decode.err" || true
if [ ! -s "$WORK/dash.pcm" ]; then
    echo "  decoder said:" >&2
    head -5 "$WORK/decode.err" | sed 's/^/    /' >&2
    fail "could not decode the DASH audio at all (not a channel-order result)"
fi
# check-tones.py exits 2 for "this is not the ladder" and 1 for a real order
# fault; collapsing both into one message is how CI came to report a channel
# mapping bug on 2026-08-10 for a session that was fine. Honour the distinction.
set +e
python3 scripts/check-tones.py 16 48000 100 100 < "$WORK/dash.pcm"
TONE_RC=$?
set -e
case "$TONE_RC" in
    0) ;;
    2) fail "decoded the WRONG STREAM, not a channel-order fault: the audio checked was not this session's tone ladder. output/ is shared with the demo loop, so the segment selection above picked the wrong chunks" ;;
    *) fail "channel order did not survive the SRT path (DASH Opus output)" ;;
esac

echo "[5/6] second concurrent caller must be rejected at the handshake"
set +e
timeout 15 docker run --rm --network "$NET" \
    -v "$PWD/$WORK:/w:ro" --entrypoint ffmpeg ambi-box-srt-gateway:local \
    -hide_banner -loglevel error -re -i /w/clip.ts -map 0 -c copy -t 4 -f mpegts \
    "srt://srt-gateway:8890?mode=caller&streamid=reject-probe&latency=2000000" \
    >/dev/null 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "second caller was accepted while a session was live"
curl -s --max-time 3 "$TEL/api/live" | grep -q "\"name\": \"$SIDNAME\"" \
    || fail "first session lost during the second-caller probe"
echo "  rejected (rc=$RC), first session intact"

echo "[6/6] teardown: operator kill must drop the LIVE session end to end"
# kill while the caller is still pushing: this exercises the whole
# enforcement chain (403 at the next 10 s update ping -> nginx drops the
# publisher -> the gateway's relay dies -> the gateway drops the SRT caller)
curl -s -X POST --max-time 5 "$TEL/api/guest/kill" >/dev/null
ENDED=0
for _ in $(seq 1 15); do
    sleep 2
    curl -s --max-time 3 "$TEL/api/live" | grep -q '"state": "live"' || { ENDED=1; break; }
done
docker rm -f "$CALLER" >/dev/null 2>&1 || true
[ "$ENDED" -eq 1 ] || fail "session still live 30s after the kill"
echo "PASS (SRT caller admitted, 16 discrete ordered channels in DASH, second caller refused, kill honoured)"
echo "note: the guest slot now carries the standard operator-kill cooldown (${GUEST_COOLDOWN_S:-300}s)"

