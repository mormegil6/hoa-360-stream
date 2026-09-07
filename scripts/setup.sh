#!/bin/sh
# One-time bootstrap: prepare .env and, by default, the OWNER contribution route.
#
# WHY OWNER IS THE DEFAULT. If you are setting this box up, you are its owner,
# and the stream you want to push is your own. The owner route is built for
# exactly that: it authenticates you with a key, and that key is a real gate
# (SRT uses it as the connection's AES key, so a caller without it is refused
# at the handshake). The guest endpoint answers a different question - "let SOMEBODY ELSE
# push to my box" - and because it takes no key at all it stays off until you
# deliberately turn it on. Setting up your own box should not require opening an
# endpoint for strangers, which is what pointing the OBS guides at the guest
# route used to imply.
#
# Safe to re-run: it never overwrites a file that already exists, so it cannot
# eat secrets you have already set.
#
# Windows: run this from Git Bash or WSL, or follow the manual steps it prints.
set -eu

cd "$(dirname "$0")/.."

# --source (alias --build): set the deployment up to BUILD the images from this
# tree instead of pulling the published ones. For working on the stack: a
# contributor who follows the normal path gets a .env pinned to the last
# release, so `docker compose up -d --build` is a no-op and their own change is
# never what runs.
SOURCE_MODE=0
for arg in "$@"; do
    case "$arg" in
        --source|--build) SOURCE_MODE=1 ;;
        -h|--help)
            echo "usage: $0 [--source]"
            echo "  --source   build images from this tree (default: pull the published release)"
            exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

ENV_FILE=".env"
OVR_FILE="docker-compose.override.yml"
# The release whose published images a fresh install runs. Bumped by
# .github/workflows/ghcr-publish.yml once the images for a tag actually exist,
# so this can never point at a tag with nothing behind it. set-version.sh
# deliberately does NOT touch it, and says so, for that same reason: this file
# previously claimed the opposite.
PIN_TAG="v1.3.5"
OWNER_PORT=8891

say()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

# A real random secret, or nothing. Never a fixed literal: a default secret in a
# public repository is not a secret, and the whole point of this script is that
# every install ends up with its own.
gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24
    elif [ -r /dev/urandom ] && command -v od >/dev/null 2>&1; then
        od -An -tx1 -N24 /dev/urandom | tr -d ' \n'
    else
        return 1
    fi
}

# ---------------------------------------------------------------------------
# 1. .env
# ---------------------------------------------------------------------------
if [ -e "$ENV_FILE" ]; then
    say "keeping your existing $ENV_FILE (not overwritten)"
    # ...but complete the two things the owner route cannot run without, where
    # doing so is provably safe. An .env made by hand (say, a renamed
    # .env.example, which real first-time setups do) predates both secrets.
    #
    # RTMP_OWNER_KEY: replaced ONLY when missing, empty, or still one of the
    # two committed, publicly-known values - those are not secrets by
    # definition, so swapping them for a real one can break nothing but an
    # attacker's luck. A value you chose yourself is never touched.
    cur_key=$(grep -m1 '^RTMP_OWNER_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
    case "${cur_key:-}" in
        ''|CHANGE_ME_this_default_is_public|hoast_demo_owner)
            k=$(gen_secret || true)
            if [ -n "${k:-}" ]; then
                if grep -q '^RTMP_OWNER_KEY=' "$ENV_FILE"; then
                    awk -v k="$k" '/^RTMP_OWNER_KEY=/ {print "RTMP_OWNER_KEY=" k; next} {print}' \
                        "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
                else
                    printf '\nRTMP_OWNER_KEY=%s\n' "$k" >> "$ENV_FILE"
                fi
                say "RTMP_OWNER_KEY was missing or still the committed public default - replaced with a fresh secret"
            fi ;;
    esac
    # LOOP_SOURCE_KEY: the same treatment, and for the same reason. It is
    # checked inside the owner application, so the committed default was a
    # working owner-publish credential for any reader of the repository until
    # 2026-08-10. rtmp-ingest now refuses to start on it, which means an .env
    # that predates this - including one left by an earlier setup.sh run, since
    # this generated only the owner key - would fail to come up without this.
    cur_loop=$(grep -m1 '^LOOP_SOURCE_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
    case "${cur_loop:-}" in
        ''|hoast_demo|CHANGE_ME_this_default_is_public)
            l=$(gen_secret || true)
            if [ -n "${l:-}" ]; then
                if grep -q '^LOOP_SOURCE_KEY=' "$ENV_FILE"; then
                    awk -v k="$l" '/^LOOP_SOURCE_KEY=/ {print "LOOP_SOURCE_KEY=" k; next} {print}' \
                        "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
                else
                    printf '\nLOOP_SOURCE_KEY=%s\n' "$l" >> "$ENV_FILE"
                fi
                say "LOOP_SOURCE_KEY was missing or still the committed public default - replaced with a fresh secret"
            fi ;;
    esac
    # SRT_PASSPHRASE was retired on 2026-08-10 in favour of role-specific names,
    # so that what you set here is what the container reads. Migrate rather than
    # leave it: the gateway refuses to start while the old name carries a value,
    # because silently ignoring it would take a passphrase-protected guest port
    # keyless with nothing said.
    if grep -q '^SRT_PASSPHRASE=' "$ENV_FILE"; then
        if grep -q '^SRT_GUEST_PASSPHRASE=' "$ENV_FILE"; then
            warn "both SRT_PASSPHRASE (retired) and SRT_GUEST_PASSPHRASE are set in $ENV_FILE;"
            warn "delete the SRT_PASSPHRASE line - the gateway will not start while it has a value"
        else
            awk '/^SRT_PASSPHRASE=/ {sub(/^SRT_PASSPHRASE=/, "SRT_GUEST_PASSPHRASE=")} {print}' \
                "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
            say "renamed the retired SRT_PASSPHRASE to SRT_GUEST_PASSPHRASE (guest listener)"
        fi
    fi
    # SRT_OWNER_PASSPHRASE: append when absent. The override below references
    # it, and the owner gateway refuses to start on an empty one, so without
    # this an existing .env means a crash-looping owner container.
    if ! grep -q '^SRT_OWNER_PASSPHRASE=' "$ENV_FILE"; then
        p=$(gen_secret || true)
        if [ -n "${p:-}" ]; then
            printf '\n# Added by scripts/setup.sh - the owner SRT route requires this.\nSRT_OWNER_PASSPHRASE=%s\n' \
                "$p" >> "$ENV_FILE"
            say "added the SRT_OWNER_PASSPHRASE the owner route requires"
        else
            warn "could not generate SRT_OWNER_PASSPHRASE (no openssl and no usable /dev/urandom);"
            warn "the owner route will not start until you set one in $ENV_FILE by hand"
        fi
    fi
    # GUEST_GW_SECRET: append when absent. It authenticates the gateways to
    # telemetry - optional defence-in-depth on the RTMP path, but MANDATORY on
    # the direct path's /gw/session/* routes, which fail closed without it
    # (there is no nginx in front of those to vouch for the caller, so the
    # address check and this secret are the whole trust anchor).
    if ! grep -q '^GUEST_GW_SECRET=' "$ENV_FILE"; then
        g=$(gen_secret || true)
        if [ -n "${g:-}" ]; then
            printf '\n# Added by scripts/setup.sh - authenticates the SRT gateways to telemetry.\nGUEST_GW_SECRET=%s\n' \
                "$g" >> "$ENV_FILE"
            say "added GUEST_GW_SECRET (gateway -> telemetry authentication)"
        else
            warn "could not generate GUEST_GW_SECRET; the direct path's session"
            warn "routes stay closed until you set one in $ENV_FILE by hand"
        fi
    fi
else
    [ -e .env.example ] || { warn "no .env.example here - run this from the repo root"; exit 1; }
    cp .env.example "$ENV_FILE"

    owner_key=$(gen_secret || true)
    owner_pass=$(gen_secret || true)
    gw_secret=$(gen_secret || true)
    loop_key=$(gen_secret || true)
    if [ -n "${owner_key:-}" ] && [ -n "${owner_pass:-}" ]; then
        # Replace the committed placeholders with secrets unique to this
        # install, and append the SRT passphrase the owner route requires.
        # Written with a temp file rather than `sed -i`, whose syntax differs
        # between GNU and BSD.
        #
        # BOTH keys, not just the owner one: LOOP_SOURCE_KEY is checked inside
        # the owner application, so its committed default was a working
        # owner-publish credential for any reader of the repository until
        # 2026-08-10, and rtmp-ingest now refuses to start on it.
        awk -v k="$owner_key" '/^RTMP_OWNER_KEY=/ {print "RTMP_OWNER_KEY=" k; next} {print}' \
            "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
        # An explicit if, not `[ -n ... ] && awk ... && mv ...`: this script runs
        # under set -e, where a false test at the head of an AND-OR list is the
        # SC2015 footgun that already bit a migration script here once.
        if [ -n "${loop_key:-}" ]; then
            awk -v k="$loop_key" '/^LOOP_SOURCE_KEY=/ {print "LOOP_SOURCE_KEY=" k; next} {print}' \
                "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
        fi
        printf '\n# Generated by scripts/setup.sh - the owner SRT route requires this.\nSRT_OWNER_PASSPHRASE=%s\n' \
            "$owner_pass" >> "$ENV_FILE"
        # .env.example ships GUEST_GW_SECRET commented out, so a fresh copy has
        # no value: append a real one, or the direct path's session routes
        # (which require it, and fail closed) stay shut on a brand-new install.
        [ -n "${gw_secret:-}" ] && printf '\n# Generated by scripts/setup.sh - authenticates the SRT gateways to telemetry.\nGUEST_GW_SECRET=%s\n' \
            "$gw_secret" >> "$ENV_FILE"
        # Pull-by-default: plain `docker compose up -d` runs the published
        # images for this release, no -f flags and no compiling. COMPOSE_FILE
        # must name the override too, because listing files explicitly turns
        # off its automatic loading. COMPOSE_PATH_SEPARATOR is pinned because
        # Windows splits COMPOSE_FILE on semicolons by default (colons belong
        # to drive letters), and this one colon-separated list must work on
        # every platform - caught live by the first Windows install of v1.0.0.
        # Only written into a FRESH .env: an existing deployment that builds
        # from source must never be switched to pulling by a setup re-run.
        if [ "$SOURCE_MODE" -eq 0 ]; then
            printf '\n# Generated by scripts/setup.sh - the stack runs from the published images\n# by default. Delete the COMPOSE_FILE line to build from source instead\n# (see README, "Building from source"). AMBI_BOX_TAG pins the release the\n# images come from; edit it to move to a newer one.\nCOMPOSE_PATH_SEPARATOR=:\nCOMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.pull.yml\nAMBI_BOX_TAG=%s\n' "$PIN_TAG"
        else
            printf '\n# Generated by scripts/setup.sh --source: no COMPOSE_FILE line, so compose\n# builds every service from this tree. Run `git submodule update --init`\n# once (the player is baked into its image at build time).\n'
        fi >> "$ENV_FILE"
        say "created $ENV_FILE with a freshly generated owner key and passphrase"
        if [ "$SOURCE_MODE" -eq 0 ]; then
            say "  (runs from the published $PIN_TAG images; --source builds from this tree instead)"
        else
            say "  (--source: compose will BUILD from this tree; run git submodule update --init first)"
        fi
    else
        warn "created $ENV_FILE, but could not generate secrets (no openssl and no usable /dev/urandom)."
        warn "Set RTMP_OWNER_KEY and SRT_OWNER_PASSPHRASE in it by hand before exposing this box."
    fi
fi

# ---------------------------------------------------------------------------
# 2. owner SRT route
# ---------------------------------------------------------------------------
if [ -e "$OVR_FILE" ]; then
    say "keeping your existing $OVR_FILE (not overwritten)"
    # ...with one exception, because "not overwritten" would otherwise mean
    # "left broken". Every override generated before 2026-08-10 passes the
    # retired SRT_PASSPHRASE to the owner gateway, which now refuses to start on
    # it rather than drop the passphrase silently. That is the right refusal and
    # the wrong experience: the file is gitignored, so a pull cannot fix it, and
    # the operator would meet a crash-looping container with no idea that a
    # generated file three weeks old was the reason. One line, in place.
    if grep -q '^[[:space:]]*-[[:space:]]*SRT_PASSPHRASE=' "$OVR_FILE"; then
        awk '{ sub(/^([[:space:]]*-[[:space:]]*)SRT_PASSPHRASE=/, "&") }
             /^[[:space:]]*-[[:space:]]*SRT_PASSPHRASE=/ { sub(/SRT_PASSPHRASE=/, "SRT_OWNER_PASSPHRASE=") }
             { print }' "$OVR_FILE" > "$OVR_FILE.tmp" && mv "$OVR_FILE.tmp" "$OVR_FILE"
        say "  migrated the retired SRT_PASSPHRASE line to SRT_OWNER_PASSPHRASE"
        say "  (recreate the service to pick it up: docker compose up -d srt-gateway-owner)"
    fi
    # Mode drift: an override written by an earlier pull-mode run pins the
    # PUBLISHED gateway image and carries no build: key, so under --source
    # srt-gateway-owner would be the last release while everything else is
    # this tree. The file is the operator's, so say it rather than rewrite it.
    if [ "$SOURCE_MODE" -eq 1 ] && grep -q '^[[:space:]]*image:[[:space:]]*ghcr\.io/' "$OVR_FILE"; then
        warn "  this override pins a published image for srt-gateway-owner, but you asked for --source."
        warn "  Replace that service's 'image:' line with the build: block from"
        warn "  docker-compose.override.yml.example, or srt-gateway-owner will run the released build."
    fi
    say "  if you want the owner route, copy the srt-gateway-owner block from"
    say "  docker-compose.override.yml.example into it."
else
    # Deliberately NOT a copy of docker-compose.override.yml.example: that
    # file binds a Tailscale address this host does not hold, so copying it
    # wholesale gives a stack that looks healthy and receives nothing. This
    # writes only the owner service.
    cat > "$OVR_FILE" <<'YAML'
# Written by scripts/setup.sh. Yours to edit; it is gitignored.
#
# The owner SRT route: push YOUR OWN 16-channel stream, authenticated, without
# opening the keyless guest endpoint. See docs/GUEST-ENDPOINT.md.
services:
  srt-gateway-owner:
YAML
    # The image line differs by mode, and the heredocs are quoted so that
    # ${AMBI_BOX_TAG} reaches compose unexpanded. Written as three appends
    # rather than one substitution: awk on macOS refuses a -v value containing
    # newlines ("newline in string"), which is how the first attempt failed.
    if [ "$SOURCE_MODE" -eq 0 ]; then
        cat >> "$OVR_FILE" <<'YAML'
    # The published image, same as the rest of the stack in pull mode.
    image: ghcr.io/mormegil6/ambisonic-box/srt-gateway:${AMBI_BOX_TAG:-latest}
YAML
    else
        cat >> "$OVR_FILE" <<'YAML'
    # Built from this tree, matching scripts/setup.sh --source.
    image: ambi-box-srt-gateway:local
    build:
      context: ./services/srt-gateway
YAML
    fi
    cat >> "$OVR_FILE" <<'YAML'
    environment:
      - SRT_ENABLED=1
      - SRT_MODE=owner
      # Direct-to-DASH: hand the probed stream straight to earshot's own
      # transcoder instead of re-encoding it to 16-ch AAC and republishing
      # over RTMP/FLV. Measured on the Mac Mini at 20 Mbps: ~28-42 % of a
      # core against the legacy ~90-130 %, and one lossy audio generation
      # deleted (OBS AAC -> Opus, with no AAC in between). Owner only; the
      # guest route keeps the RTMP hop, which is where guest admission and
      # the kick lever live. Set SRT_DIRECT=0 in .env to fall back.
      - SRT_DIRECT=${SRT_DIRECT:-1}
      # Authenticates this gateway to telemetry's /gw/session/* routes. The
      # owner container is a DIFFERENT container from srt-gateway, so it needs
      # its own copy; telemetry tells the two apart by peer address and will
      # refuse a claim that arrives without a matching secret.
      - GUEST_GW_SECRET=${GUEST_GW_SECRET:-}
      - RTMP_OWNER_KEY=${RTMP_OWNER_KEY}
      - SRT_OWNER_PASSPHRASE=${SRT_OWNER_PASSPHRASE}   # mandatory in owner mode
      - SRT_LATENCY_MS=${SRT_LATENCY_MS:-2000}
      - HOME=/tmp
      - XDG_CACHE_HOME=/tmp
      - GST_REGISTRY=/tmp/gst-registry.bin
    ports:
      # All interfaces, matching the stack's other two contribution ports
      # (RTMP 1935 and guest SRT 8890 both bind 0.0.0.0 in docker-compose.yml).
      # A streaming server that can only be reached from itself is the rare
      # case, not the common one, so this is not loopback.
      #
      # What actually gates this port is the passphrase, and it is a real gate:
      # SRT uses it as the connection's AES key, so libsrt refuses the
      # handshake before a byte is parsed. setup.sh generated a 192-bit one.
      # Of the three contribution ports this is the best protected - 8890
      # admits guests with no key at all once GUEST_ENABLED=1.
      #
      # To stream in from outside, forward UDP 8891 to this host on your
      # router, then point OBS at srt://<your-public-address>:8891 with the
      # same streamid and passphrase.
      #
      # Narrow it if you want less exposure: 127.0.0.1 for this machine only,
      # or a VPN address (Tailscale/WireGuard) to keep it off the internet.
      # Do NOT write a public IP this host does not itself hold - behind NAT
      # that is your router's address, and the bind will look healthy while
      # receiving nothing. On a host with net.ipv4.ip_nonlocal_bind=1 (which
      # docs/ENDPOINTS.md recommends, for an unrelated boot race) that bind
      # does not even error. `ip -4 addr show scope global` lists what is
      # really here.
      - "0.0.0.0:8891:8890/udp"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    init: true
    depends_on:
      rtmp-ingest:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python3", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health', timeout=2)"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 5s
    restart: unless-stopped
YAML
    say "created $OVR_FILE with the owner SRT route on all interfaces, port $OWNER_PORT"
fi

# ---------------------------------------------------------------------------
# 3. preflight: say what will collide, before `up` says it worse
# ---------------------------------------------------------------------------
# Two failures this project has actually hit, both of which look like something
# else at the time:
#
#   - A previous stack still running. On the Pi (2026-08-10) five containers
#     from the pre-rename `hoa360` project were up for two days holding 1935,
#     8080, 8081, 8090 and 8890. `up` would have failed on a port bind, which
#     reads as a firewall or permissions problem rather than "you already have
#     one of these running".
#   - Leftovers from the 2026-08-08 project rename. Renaming a Compose project
#     renames its VOLUMES, so a naive `up -d` silently creates empty ones and
#     orphans the old telemetry history rather than failing.
#
# Advisory, never fatal: this reports, and leaves the decision to the operator.
# Skipped entirely without a working docker, since setup.sh is otherwise usable
# on a machine that has none yet.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    # Ports come from compose rather than a list here, so they cannot drift.
    ports=$(docker compose config --format json 2>/dev/null | python3 -c "
import json,sys
try:
    c = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for name, svc in (c.get('services') or {}).items():
    for p in (svc.get('ports') or []):
        pub = p.get('published')
        if pub:
            print('%s %s %s' % (pub, p.get('protocol', 'tcp'), name))
" 2>/dev/null || true)

    if [ -n "${ports:-}" ]; then
        # Whatever is here: ss on Linux, lsof on macOS. If neither, say so
        # rather than either claiming the ports are free or saying nothing.
        #
        # busybox's lsof is explicitly NOT usable: it takes no options at all
        # ("Usage: lsof / Show all open files"), so it ignores the query below,
        # exits 0, and every single port comes back "already in use". Verified
        # on alpine 3.24 / busybox 1.37: six false warnings on a host with no
        # containers running. A capability test, not a name test.
        if command -v ss >/dev/null 2>&1; then
            probe=ss
        elif command -v lsof >/dev/null 2>&1 && ! lsof --help 2>&1 | grep -qi busybox; then
            probe=lsof
        else
            probe=none
        fi

        if [ "$probe" = none ]; then
            warn "  (no usable ss or lsof here, so nothing was checked for port collisions;"
            warn "   'docker compose up' is what will tell you about a busy port)"
        fi

        if [ "$probe" != none ]; then
            echo "$ports" | while read -r port proto svc; do
                [ -n "${port:-}" ] || continue
                busy=""
                if [ "$probe" = ss ]; then
                    if [ "$proto" = udp ]; then
                        ss -lnu 2>/dev/null | awk '{print $5}' | grep -qE "[:.]$port\$" && busy=1
                    else
                        ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$" && busy=1
                    fi
                else
                    if [ "$proto" = udp ]; then
                        lsof -nP -iUDP:"$port" >/dev/null 2>&1 && busy=1
                    else
                        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && busy=1
                    fi
                fi
                # An explicit if, not `[ -n ... ] && warn`: under set -e a false
                # test as the last command of a loop body is the SC2015 footgun
                # that has already bitten a script in this repository.
                if [ -n "$busy" ]; then
                    warn "  port $port/$proto is already in use, and $svc wants it"
                fi
            done
        fi
    fi

    # Containers and volumes from a differently-named project. `ambi-box` has
    # been the project name since 2026-08-08; anything else here is a leftover.
    others=$(docker ps -a --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
             | grep -v '^$' | grep -v '^ambi-box$' | sort -u || true)
    if [ -n "${others:-}" ]; then
        warn ""
        warn "Containers from another Compose project are present: $(echo "$others" | tr '\n' ' ')"
        warn "If that is an older copy of THIS stack, stop it first (it holds the ports),"
        warn "and note that its volumes keep the old project's name: a fresh 'up' will"
        warn "create empty ones rather than reusing them. docs/DEPLOYMENT.md has the"
        warn "copy-the-volume-across procedure."
    fi
fi

# ---------------------------------------------------------------------------
# 4. what to do next
# ---------------------------------------------------------------------------
pass_now=$(grep -m1 '^SRT_OWNER_PASSPHRASE=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)

say ""
say "Next:"
say "  docker compose up -d"
say ""
say "Then point OBS at (see docs/obs-windows.md or docs/obs-macos.md for the rest):"
if [ -n "${pass_now:-}" ]; then
    say "  srt://<address-you-reach-this-box-on>:$OWNER_PORT?streamid=owner&passphrase=$pass_now&latency=2000000&pkt_size=1128"
else
    say "  srt://<address-you-reach-this-box-on>:$OWNER_PORT?streamid=owner&passphrase=<SRT_OWNER_PASSPHRASE>&latency=2000000&pkt_size=1128"
fi
say ""
say "The owner route listens on all interfaces, like the stack's other two"
say "contribution ports. Testing on this machine: use 127.0.0.1. Streaming in"
say "from outside: forward UDP $OWNER_PORT to this host and use your public"
say "address. Either way the URL is otherwise identical."
say ""
say "That passphrase is in $ENV_FILE, which is gitignored. It is what gates the"
say "port - SRT uses it as the connection's AES key, so a caller without it is"
say "refused at the handshake. To narrow the bind instead, see $OVR_FILE."
say ""
say "Letting OTHER people push to this box is a separate, keyless route that is"
say "off by default: set GUEST_ENABLED=1 in $ENV_FILE. Read the trade-offs first"
say "in docs/GUEST-ENDPOINT.md - it takes no password from anyone."
