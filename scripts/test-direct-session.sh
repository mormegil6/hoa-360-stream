#!/usr/bin/env bash
# Conformance suite for the DIRECT contribution path's session protocol
# (telemetry's /gw/session/claim|beat|done, and earshot's peer-gated
# listeners). Design and rationale: guest-direct-dash-design.md in the
# deployment repo.
#
# WHY IT EXISTS SEPARATELY from scripts/test-guest-endpoint.sh: that suite
# asserts a guest is publishing by looking for one on rtmp-ingest's stat page,
# and a direct session appears in NO stat page - the whole point is that it
# skips RTMP. Run against a direct session those assertions fail while
# everything actually works, which is worse than not testing at all.
#
# WHAT IT COVERS, and does not: this is the CONTROL plane end to end - real
# containers, real HTTP, the real authentication - with no SRT and no media.
# That is deliberate: every finding this suite exists to catch (admission,
# kick, preemption ordering, restart recovery, the trust anchor, the flood
# floor) lives in the control plane, and driving it directly makes the suite
# fast, deterministic and runnable on a laptop. The MEDIA path (a real SRT
# push, thermal behaviour, a real listener wedge) is certified separately on
# the box - see the deployment repo's PLAN.
#
# Usage:  ./scripts/test-direct-session.sh
# Needs:  the stack up, GUEST_ENABLED=1 and GUEST_GW_SECRET set in .env.
# Leaves: the guest slot free and the owner latch clear.
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT="$(grep -m1 '^name:' docker-compose.yml | awk '{print $2}')"
PROJECT="${PROJECT:-ambi-box}"
GUEST_GW="${PROJECT}-srt-gateway-1"
OWNER_GW="${PROJECT}-srt-gateway-owner-1"
TELEM="${PROJECT}-telemetry-1"
SECRET="$(grep -m1 '^GUEST_GW_SECRET=' .env 2>/dev/null | cut -d= -f2-)"

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n'   "$1"; fail=$((fail+1)); }
note() { printf '       %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

[ -n "$SECRET" ] || { echo "GUEST_GW_SECRET is not set in .env - the session routes fail closed without it."; exit 1; }
docker ps --format '{{.Names}}' | grep -q "^${GUEST_GW}$" || { echo "guest gateway container ${GUEST_GW} is not running"; exit 1; }

# One helper for every call: run it FROM a container, because the identity
# telemetry authenticates is the peer address of the connection, so where the
# call originates is the thing under test.
call() {  # call <container> <path-with-query>  ->  "<status> <body>"
    docker exec -i -e S="$SECRET" -e P="$2" "$1" python3 - <<'PY' 2>/dev/null
import urllib.request, urllib.error, os
u = f"http://telemetry:8090{os.environ['P']}"
u += ("&" if "?" in u else "?") + "gw=" + os.environ["S"]
try:
    r = urllib.request.urlopen(u, timeout=30)
    print(r.status, (r.read() or b"").decode()[:200])
except urllib.error.HTTPError as e:
    print(e.code, (e.read() or b"").decode()[:200])
except Exception as e:
    print("000", e)
PY
}
code() { echo "$1" | awk '{print $1}'; }
sid_of() { echo "$1" | grep -o '"session": *"[a-f0-9]*"' | grep -o '[a-f0-9]\{8,\}'; }

reset_slot() {
    # STOP telemetry first, then edit, then start. Editing the state file
    # under a running telemetry is a race it usually wins: any _guest_save()
    # between the write and the restart puts the old cooldown straight back,
    # and the next section then fails for a reason that has nothing to do
    # with what it is testing. sed rather than python3 because the helper
    # image is the small one that is already pulled.
    docker stop "$TELEM" >/dev/null 2>&1
    docker run --rm -v "${PROJECT}_telemetry-data:/data" alpine:3.11 sh -c '
      f=/data/guest_state.json
      [ -f "$f" ] || exit 0
      sed -e "s/\"state\": *\"[a-z]*\"/\"state\": \"free\"/" \
          -e "s/\"cooldown_until\": *[0-9.]*/\"cooldown_until\": null/" \
          -e "s/\"direct\": *true/\"direct\": false/" \
          -e "s/\"kill\": *true/\"kill\": false/" \
          -e "s/\"session\": *\"[a-f0-9]*\"/\"session\": null/" \
          -e "s/\"grace_started\": *[0-9.]*/\"grace_started\": null/" \
          -e "s/\"terminating\": *\"[^\"]*\"/\"terminating\": null/" \
          "$f" > "$f.tmp" && mv "$f.tmp" "$f"' >/dev/null 2>&1
    docker start "$TELEM" >/dev/null 2>&1
    for _ in $(seq 1 30); do
        sleep 1
        r=$(call "$GUEST_GW" "/rtmp/guest/precheck-snapshot")
        echo "$r" | grep -q '"available": true' && return 0
    done
    return 1
}

echo "direct-session conformance  (project: $PROJECT)"
reset_slot || { echo "telemetry did not come back after reset"; exit 1; }

# --- 1. the trust anchor -----------------------------------------------------
head_ "1. trust anchor (BLOCKER 1 / BLOCKER-owner)"
r=$(docker run --rm --network "${PROJECT}_default" alpine:3.11 sh -c \
      "apk add --update curl >/dev/null 2>&1; curl -s -o /dev/null -w '%{http_code}' \
       'http://telemetry:8090/gw/session/claim?role=guest&name=x&ip=1.2.3.4&tracks=4&gw=$SECRET'" 2>/dev/null)
[ "$r" = "403" ] && ok "a non-gateway container is refused even WITH the correct secret" \
                 || bad "non-gateway container got $r, expected 403"

r=$(docker exec -i -e P="/gw/session/claim?role=guest&name=x&ip=1.2.3.4&tracks=4&gw=wrongsecret" "$GUEST_GW" python3 - <<'PY' 2>/dev/null
import urllib.request, urllib.error, os
try:
    print(urllib.request.urlopen("http://telemetry:8090"+os.environ["P"], timeout=10).status)
except urllib.error.HTTPError as e: print(e.code)
PY
)
[ "$r" = "403" ] && ok "the real gateway is refused with a WRONG secret" \
                 || bad "wrong secret got $r, expected 403"

r=$(docker run --rm --network "${PROJECT}_default" alpine:3.11 sh -c \
      "apk add --update curl >/dev/null 2>&1; curl -s -o /dev/null -w '%{http_code}' \
       'http://telemetry:8090/rtmp/owner/notify?name=evil'" 2>/dev/null)
[ "$r" = "404" ] && ok "a stranger cannot latch owner state through the /rtmp alias" \
                 || bad "alias from a stranger got $r, expected 404"

# the regression that shipped and was caught: the gateway is ALSO a legitimate
# caller of /rtmp/*, and gating on rtmp-ingest alone broke every SRT guest
r=$(call "$GUEST_GW" "/rtmp/guest/precheck-snapshot")
[ "$(code "$r")" = "200" ] && ok "the gateway's own precheck poll is admitted (regression guard)" \
                           || bad "gateway precheck got $(code "$r"), expected 200"

# --- 2. admission ------------------------------------------------------------
head_ "2. guest admission and the single slot"
r=$(call "$GUEST_GW" "/gw/session/claim?role=guest&name=conformance&ip=203.0.113.11&tracks=4")
GSID=$(sid_of "$r")
[ "$(code "$r")" = "200" ] && [ -n "$GSID" ] && ok "a guest claim is admitted and returns a session id" \
                                             || bad "guest claim: $r"

r=$(call "$GUEST_GW" "/gw/session/claim?role=guest&name=second&ip=198.51.100.22&tracks=4")
c=$(code "$r"); { [ "$c" = "403" ] || [ "$c" = "409" ]; } \
    && ok "a second guest is refused while the slot is busy ($c)" \
    || bad "second guest got $c, expected 403 or 409"

r=$(call "$GUEST_GW" "/gw/session/beat?session=$GSID")
[ "$(code "$r")" = "200" ] && ok "the admitted session beats 200" || bad "beat: $r"

sleep 1.5   # the beat floor is 1 s per peer by design; see section 6
r=$(call "$GUEST_GW" "/gw/session/beat?session=deadbeefdeadbeef")
[ "$(code "$r")" = "410" ] && ok "an unknown session id gets 410 reclaim, never 403 (BLOCKER 4)" \
                           || bad "unknown session got $(code "$r"), expected 410"

# --- 3. the kick lever -------------------------------------------------------
head_ "3. the kick lever (what makes a guest safe to admit)"
docker exec "$TELEM" python3 -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8090/api/guest/kill', data=b'', timeout=10)" >/dev/null 2>&1
sleep 1.5
r=$(call "$GUEST_GW" "/gw/session/beat?session=$GSID")
[ "$(code "$r")" = "403" ] && ok "the dashboard kill reaches the session as a 403 verdict" \
                           || bad "beat after kill got $(code "$r"), expected 403"
call "$GUEST_GW" "/gw/session/done?session=$GSID" >/dev/null
sleep 1

# --- 4. preemption -----------------------------------------------------------
head_ "4. owner preemption of a live direct guest (ordering)"
reset_slot >/dev/null
r=$(call "$GUEST_GW" "/gw/session/claim?role=guest&name=victim&ip=203.0.113.5&tracks=4")
GSID=$(sid_of "$r")
if [ -z "$GSID" ]; then
    bad "could not seat a guest for the preemption test: $r"
else
    ok "a guest holds the slot"
    r=$(call "$OWNER_GW" "/gw/session/claim?role=owner&name=owner&ip=10.0.0.1&tracks=4")
    [ "$(code "$r")" = "409" ] \
        && ok "the owner is told 409 retry, NOT 200, while the guest still writes" \
        || bad "owner claim during a live guest got $(code "$r"), expected 409"
    sleep 1.5
    r=$(call "$GUEST_GW" "/gw/session/beat?session=$GSID")
    [ "$(code "$r")" = "403" ] && ok "the preempted guest's beat carries the kill" \
                               || bad "preempted guest beat got $(code "$r"), expected 403"
    echo "$r" | grep -q "owner broadcast" \
        && ok "and it carries the no-cooldown reason (the guest is innocent)" \
        || note "reason not visible in the body: $r"
    call "$GUEST_GW" "/gw/session/done?session=$GSID" >/dev/null
    sleep 2
    r=$(call "$OWNER_GW" "/gw/session/claim?role=owner&name=owner&ip=10.0.0.1&tracks=4")
    OSID=$(sid_of "$r")
    [ "$(code "$r")" = "200" ] && ok "the owner is admitted once the guest's writer is gone" \
                               || bad "owner retry got $(code "$r"), expected 200"
    [ -n "$OSID" ] && call "$OWNER_GW" "/gw/session/done?session=$OSID" >/dev/null
fi

# --- 5. reserved-name refusal ------------------------------------------------
head_ "5. an owner may not claim the demo loop's own name"
r=$(call "$OWNER_GW" "/gw/session/claim?role=owner&name=$(grep -m1 '^DASH_NAME=' .env 2>/dev/null | cut -d= -f2- || echo hoast_demo)&ip=10.0.0.2&tracks=4")
[ "$(code "$r")" = "403" ] \
    && ok "refused: owner_notify latches nothing for LOOP_NAME, so 200 would be a lie" \
    || bad "reserved-name claim got $(code "$r"), expected 403"

# --- 6. backpressure ---------------------------------------------------------
head_ "6. backpressure (the 2026-08-09 flood shape)"
r=$(docker exec -i -e S="$SECRET" "$GUEST_GW" python3 - <<'PY' 2>/dev/null
import urllib.request, urllib.error, os
S=os.environ["S"]; n429=0
for _ in range(30):
    try:
        urllib.request.urlopen(f"http://telemetry:8090/gw/session/beat?session=x&gw={S}", timeout=5)
    except urllib.error.HTTPError as e:
        if e.code == 429: n429 += 1
    except Exception:
        pass
print(n429)
PY
)
[ "${r:-0}" -ge 20 ] && ok "a beat flood is refused ($r/30 answered 429)" \
                     || bad "only $r/30 beats were throttled"
r=$(call "$GUEST_GW" "/rtmp/guest/precheck-snapshot")
[ "$(code "$r")" = "200" ] && ok "telemetry is still serving after the flood" \
                           || bad "telemetry did not survive the flood"

# --- 7. listener admission ---------------------------------------------------
head_ "7. earshot's listeners admit only the gateways (BLOCKER 2)"
docker run --rm --network "${PROJECT}_default" alpine:3.11 sh -c \
  "apk add --update netcat-openbsd >/dev/null 2>&1; echo squat | timeout 3 nc earshot 9100" >/dev/null 2>&1
sleep 2
if docker exec "${PROJECT}-earshot-1" sh -c "tail -20 /tmp/nginx_rtmp_ffmpeg_log 2>/dev/null" | grep -q "rejected connection"; then
    ok "a non-gateway TCP connection to :9100 is rejected and logged"
else
    bad "no rejection logged for the squatter connection"
fi
if docker exec "${PROJECT}-earshot-1" sh -c "netstat -tln 2>/dev/null | grep -c ':9100'" | grep -q '^1$'; then
    ok "and the listener re-armed afterwards (a prober cannot wedge the port)"
else
    bad ":9100 is not listening after the rejected connection"
fi

# All four, not just the one the squatter hit: the port carries the video codec
# as well as the track count, and a listener that never armed fails only for the
# codec nobody happened to push that day.
missing=""
for p in 9100 9101 9102 9103; do
    docker exec "${PROJECT}-earshot-1" sh -c "netstat -tln 2>/dev/null | grep -q ':$p'" \
        || missing="$missing $p"
done
[ -z "$missing" ] && ok "all four direct listeners are armed (9100-9103)" \
                  || bad "direct listeners not armed:$missing"

reset_slot >/dev/null
printf '\n\033[1m%d passed, %d failed\033[0m\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
