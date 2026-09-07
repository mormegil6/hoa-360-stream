#!/usr/bin/env python3
# ambisonic-box telemetry: containerised collector + alerter + dashboard server.
#
# Runs as the `telemetry` compose service. Every INTERVAL seconds it:
#   - reads container health + the player access log via the mounted docker socket,
#   - reads stream liveness from the shared dash-output volume and earshot's /stat,
#   - reads CPU temp / disk from optional host mounts (degrades to null if absent),
#   - writes stats.json (private dashboard) + status.json (curated, for the
#     stream page) + a viewers.csv history,
#   - fires Telegram on the RISING edge of a *sustained* problem (debounced) and on
#     recovery. Telegram is optional (skipped unless BOT_TOKEN/CHAT_ID are set).
#
# A tiny threaded HTTP server serves the dashboard on TEL_PORT. stdlib only + the
# docker CLI (installed in the image; the socket is mounted read-write, because
# on-demand idling starts and stops the loop-source container).
import hashlib, hmac, json, os, re, socket, subprocess, time, ipaddress, threading, uuid
import http.server, socketserver, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST     = os.environ.get("TEL_HOST", "ambisonic-box")
def _own_project():
    """The compose project this telemetry container actually belongs to, read
    from its own labels (hostname == container id). The env var is only a
    fallback: it cannot see a `docker compose -p <name>` override, and a
    hardcoded value once broke loop control under any renamed project."""
    try:
        import socket
        out = sh(f"docker inspect {socket.gethostname()} "
                 "--format '{{index .Config.Labels \"com.docker.compose.project\"}}'").strip()
        if out and out != "<no value>":
            return out
    except Exception:
        pass
    return os.environ.get("COMPOSE_PROJECT_NAME", "ambi-box")

PROJECT  = _own_project()
DATA     = Path(os.environ.get("TEL_DATA", "/data"))          # persisted volume + web root
DASH     = Path(os.environ.get("TEL_DASH", "/dash"))          # shared dash-output (ro)
THERMAL  = os.environ.get("TEL_THERMAL", "/sys/class/thermal/thermal_zone0/temp")  # container sees host sysfs
DISKPATH = os.environ.get("TEL_DISK", "/host/root")           # host / mounted ro (optional)
EARSHOT  = os.environ.get("TEL_EARSHOT", "http://earshot/stat")
PORT     = int(os.environ.get("TEL_PORT", "8090"))
INTERVAL = int(os.environ.get("TEL_INTERVAL", "60"))
BOT      = os.environ.get("BOT_TOKEN", "").strip()
CHAT     = os.environ.get("CHAT_ID", "").strip()

VIEWER_WINDOW = 90; ENCODER_MIN = 0.90; SEG_STALE_S = 15
TEMP_CRIT_C = 100; DISK_FULL_PCT = 90; DEBOUNCE = 2

# On-demand source: idle the loop when nobody is watching, let a visitor restart it.
# RFC 6598 shared address space, which Tailscale hands out. Python's is_private
# returns False for it, so without this an operator watching over the VPN is
# counted as public audience and inflates the figure shown to real visitors.
CGNAT = ipaddress.ip_network("100.64.0.0/10")

SOURCE_SVC   = os.environ.get("TEL_SOURCE_SVC", "loop-source")
IDLE_STOP_MIN = int(os.environ.get("TEL_IDLE_STOP_MIN", "10"))   # 0 disables idling
START_GRACE_S = int(os.environ.get("TEL_START_GRACE_S", "300"))  # never idle just after a start
PUBDIR = Path(os.environ.get("TEL_PUB", "/pub"))   # shared with hoast-player (public)
STATS = DATA/"stats.json"; PUB = PUBDIR/"status.json"
CSV   = DATA/"viewers.csv"; STATE = DATA/"alert_state.json"
VODCSV = DATA/"vod_analytics.csv"   # 24h-window gauge rows, one per fresh poll
IRSTATE = DATA/"renderer_state.json"   # rolling 24h renderer-start dedup, hashed
IR_WINDOW_S = 86400                 # 24 h, a gauge like the VOD arrivals tile


def run(cmd, t=12):
    """(ok, stdout). ok is False when the command could not be run at all or
    exited non-zero. Callers that act on the OUTPUT must not confuse that with
    "ran fine, said nothing": a docker probe that times out reads as an empty log,
    an empty log reads as nobody watching, and nobody watching stops the stream."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return p.returncode == 0, p.stdout
    except Exception:
        return False, ""

def sh(cmd, t=12):
    """Output only, for probes where empty and failed mean the same thing."""
    return run(cmd, t)[1]

def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")

def docker_ps(include_stopped=False):
    """Compose-managed containers. include_stopped adds `-a`.

    The health path needs the stopped ones. Without `-a`, `docker ps` returns
    only what is RUNNING, and services() emits one row per returned row, so a
    container that exits does not become "down" - it VANISHES from the list,
    and services_down, which is `any not healthy`, sees an empty set and stays
    quiet. The alert therefore fired for a merely unhealthy service and was
    silent for a dead one, which is the worse failure. Observed on the box on
    2026-08-11 with loop-source exited and the services list empty.
    source_start already carried its own `-a` variant for the same reason.
    """
    flag = "-a " if include_stopped else ""
    out = sh(f'docker ps {flag}--filter "label=com.docker.compose.project={PROJECT}" --format "{{{{json .}}}}"')
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows

_expected_cache = [0.0, ()]

def expected_services():
    """Services compose says exist for THIS configuration, minus the ones
    services() deliberately does not police.

    Asked of compose rather than hardcoded, so an install without the owner
    override does not get a permanent false alarm for srt-gateway-owner, and
    so a profile-gated service (shaka) is absent exactly when it is inactive.
    Cached for a minute: it is a subprocess, and it changes only when the
    compose files do.
    """
    now = time.time()
    if now - _expected_cache[0] < 60 and _expected_cache[1]:
        return _expected_cache[1]
    out = sh("docker compose config --services")
    names = tuple(n for n in (l.strip() for l in out.splitlines())
                  if n and n not in SERVICES_UNPOLICED
                  and not (n == "srt-gateway" and not SRT_ENABLED))
    if names:                      # a failed call must not empty the set
        _expected_cache[0], _expected_cache[1] = now, names
    return _expected_cache[1]

def svc_label(row):
    for kv in (row.get("Labels", "") or "").split(","):
        if kv.startswith("com.docker.compose.service="):
            return kv.split("=", 1)[1]
    return row.get("Names", "")

def services(ps):
    rows = []
    for r in ps:
        # compose-managed long-lived containers ONLY: compose bakes the
        # service label into the IMAGE, so a stray `docker run` from e.g. the
        # earshot image shows up wearing service=earshot (seen live as a
        # doubled pill when a mock guest pusher ran from that image). The
        # oneoff=False label exists only on real `up`-managed containers;
        # same bug family as the arbiter's source_container filter.
        if "com.docker.compose.oneoff=False" not in (r.get("Labels", "") or ""):
            continue
        name = svc_label(r)
        if name in SERVICES_UNPOLICED:                      # self + non-core live path
            continue
        # srt-gateway runs even with SRT_ENABLED=0 (it just never binds the
        # UDP port), and a green pill for a service that is deliberately doing
        # nothing reads as noise. Same rule as the two above: only list what is
        # actually part of the live path in THIS configuration.
        if name == "srt-gateway" and not SRT_ENABLED:
            continue
        state, status = r.get("State", ""), r.get("Status", "")
        health = ("healthy" if "(healthy)" in status else
                  "unhealthy" if "(unhealthy)" in status else
                  "starting" if "starting" in status else "")
        rows.append({"name": name, "state": state, "health": health,
                     "healthy": state == "running" and health not in ("unhealthy", "starting")})
    # A service compose declares but docker knows nothing about - never created,
    # or `docker rm`'d - produces no row above at all, so it would still be
    # invisible even with -a. Synthesise one so it reads as down rather than as
    # absent, which is the whole point of this function.
    seen = {r["name"] for r in rows}
    for name in expected_services():
        if name not in seen:
            rows.append({"name": name, "state": "missing", "health": "",
                         "healthy": False})
    return rows

def container_named(ps, service):
    for r in ps:
        if svc_label(r) == service:
            return r.get("Names", "")
    return ""

def temp_c():
    try:
        return round(int(Path(THERMAL).read_text().strip()) / 1000)
    except Exception:
        return None

def disk_pct():
    m = re.search(r"\s(\d+)%\s", sh(f"df -P {DISKPATH}"))
    return int(m.group(1)) if m else None

def mem_pct():
    """Host memory in use. /proc/meminfo inside the container is the host's, so
    this needs no mount. Based on MemAvailable rather than MemFree: page cache is
    reclaimable, and MemFree alone reads as ~100% used on any warm machine."""
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if v.strip():
                info[k] = int(v.split()[0])      # kB
        total, avail = info.get("MemTotal"), info.get("MemAvailable")
        if not total or avail is None:
            return None
        return round((total - avail) * 100 / total)
    except Exception:
        return None

def uptime_s():
    try:
        return int(float(Path("/host/uptime").read_text().split()[0]))
    except Exception:
        try:
            return int(float(Path("/proc/uptime").read_text().split()[0]))
        except Exception:
            return None

def load1():
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        return None

def segment_age():
    """Age of the freshest segment of the STALEST track.

    Globbing chunk-stream*.webm alone is wrong whenever the video is
    stream-copied: the segment container follows the video codec's native one
    and audio follows video, so under -dash_segment_type mp4 ffmpeg's dash
    muxer writes BOTH tracks as fragmented MP4 (chunk-stream0-*.m4s video,
    chunk-stream1-*.m4s Opus-in-fMP4), and a *.webm glob matches nothing at
    all: a live stream reads as offline and the stall alert can never fire.
    VP9 puts both tracks back in WebM, which is why all three extensions get
    swept. Taking the max
    across representations means either track going quiet marks the stream down.
    """
    newest = {}
    now = time.time()
    try:
        for pat in ("chunk-stream*.webm", "chunk-stream*.m4s", "chunk-stream*.mp4"):
            for p in DASH.glob(pat):
                m = re.match(r"chunk-stream(\d+)-", p.name)
                if not m:
                    continue
                age = now - p.stat().st_mtime
                rep = m.group(1)
                if rep not in newest or age < newest[rep]:
                    newest[rep] = age
    except Exception:
        return None
    return round(max(newest.values())) if newest else None


def stream_state():
    x = sh(f"curl -s --max-time 5 {EARSHOT}")
    # The RTMP stat is the source of truth for RTMP publishers. An SRT owner on
    # the DIRECT path (gateway -> earshot's mpegts listener, 2026-08-09; design
    # in the deployment repo) never appears there - the whole point is skipping
    # the RTMP/FLV hop - but the gateway latches _owner all the same: through
    # /gw/session/claim (which calls owner_notify) when GUEST_GW_SECRET is set,
    # refreshed by a beat every 10 s, or through the legacy /rtmp/owner/notify
    # alias the ingest callbacks use, re-notified every 30 s, when it is not.
    # So the latch IS the publishing signal for that path. A stale latch
    # (owner_tick clears it within ~2 cycles) briefly reports publishing with
    # aging segments; `live` stays false then, and auto_idle's source_stop on
    # an already-stopped loop is a no-op, so the residue is cosmetic.
    # A direct GUEST is invisible to the stat page for the same reason, and
    # has no _owner latch to ride: without this term the badge reads OFFLINE
    # while its segments flow, the stall alert can never fire, and - the hole
    # the 2026-08-09 spike demonstrated from the public internet - source_start
    # sees a free slot and starts the demo loop beside it on the single-writer
    # DASH tree. Only "live" counts, never "handover": guest_publish sets
    # handover BEFORE _earshot_unwound runs, and this same flag is what that
    # unwind waits on, so counting handover would deadlock admission against
    # itself.
    publishing = ("<publishing/>" in x or _owner["live"]
                  or (_guest["direct"] and _guest["state"] == "live"))
    m = re.search(r"<nclients>(\d+)</nclients>", x)
    nclients = int(m.group(1)) if m else 0
    seg_age = segment_age()
    live = publishing and seg_age is not None and seg_age < SEG_STALE_S
    return {"publishing": publishing, "nclients": nclients, "segment_age_s": seg_age, "live": live}

_enc_prev = [0.0, None]     # (wall clock, ffmpeg media time) of the previous poll

def encoder(ps, publishing):
    """Encoder progress, reported two ways.

    `speed` is ffmpeg's own figure, which is a CUMULATIVE average of pts over
    wall clock since process start. After an hour at 1.0x, a collapse to 0.3x
    needs ten more minutes to drag that average under ENCODER_MIN, and a milder
    0.85x collapse never gets there at all. That is the exact shape of thermal
    throttling, a classic failure shape on thermally constrained hosts, and nothing else catches it:
    a throttled encoder still writes segments inside SEG_STALE_S, so
    stream_stalled stays quiet too.

    So `behind` is decided on `speed_now`: media time written divided by wall
    clock elapsed between two consecutive polls. `speed` is kept because the
    dashboard shows it and the lifetime average is still worth seeing.
    """
    if not publishing:
        _enc_prev[1] = None          # a restart must not be differenced across
        return {"speed": None, "speed_now": None, "behind": False}
    c = container_named(ps, "earshot")
    # tail -c rather than grep over the whole file: nginx-no-ssl.conf opens this
    # log with 2>> and never truncates it, so it reaches tens of MB in a
    # streaming day and was being read end to end every INTERVAL seconds. ffmpeg
    # separates progress updates with \r, so the last few KB always hold several.
    out = sh(f'docker exec {c} sh -c "tail -c 4096 /tmp/nginx_rtmp_ffmpeg_log 2>/dev/null"') if c else ""
    sps = re.findall(r"speed=\s*([0-9.]+)x", out)
    sp = float(sps[-1]) if sps else None
    ts = re.findall(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)", out)
    media = (int(ts[-1][0]) * 3600 + int(ts[-1][1]) * 60 + float(ts[-1][2])) if ts else None
    rate, now = None, time.time()
    if media is not None:
        # Media time going backwards means a new ffmpeg process appended to the
        # same log, so the pair straddling the restart is meaningless. Skip one
        # poll rather than report a nonsense rate.
        if _enc_prev[1] is not None and media > _enc_prev[1] and now > _enc_prev[0]:
            rate = round((media - _enc_prev[1]) / (now - _enc_prev[0]), 2)
        _enc_prev[0], _enc_prev[1] = now, media
    return {"speed": sp, "speed_now": rate,
            "behind": rate is not None and rate < ENCODER_MIN}
# NOTE for anything rendering these: `speed` is ffmpeg's LIFETIME AVERAGE and
# `speed_now` is the media-time advance per wall-clock second, which is what
# `behind` is computed from. They diverge during a restart or a stall, and an
# alert once read "encoder behind realtime (1.05x)" - a sentence that
# contradicts itself, because it quoted the average while the verdict came from
# the instantaneous rate. Show the number the verdict used, or show both.

# HISTORY, not the current rule: until 2026-08-09 a viewer was somebody
# FETCHING SEGMENTS, not somebody holding a connection.
# dash.js re-reads the manifest on a timer whether or not anything is playing,
# so matching any /dash/ path counted an abandoned browser tab as an audience
# forever. Measured 2026-08-07: one VPS-hosted client played 15 s, then polled
# hoast_demo.mpd 2748 times over the next 5.5 h and was counted as a viewer
# throughout. That was not merely a wrong number on a panel - auto_idle reads
# this, so the box kept transcoding 4K for nobody. `chunk-stream` is the same
# marker the segment-freshness probe already keys on, and the VOD packager
# names its files v_<res>.mp4 / audio_16ch.webm, so this cannot pick up /vod/.
SEGMENT_MARK = "chunk-stream"
# The binaural impulse responses, requested by the HOAST360 renderer when it
# initialises its convolvers and by nothing else on the site. o1..o4, so the
# match stays order-agnostic. This answers a different question from "viewers":
# not who is connected, but for whom the ambisonic path actually ran. Repeat
# listeners are visible too - the files carry ETag/Last-Modified but no
# Cache-Control, so a returning browser revalidates and still hits the log
# (measured over 7 days: 28 x 200 alongside 27 x 304).
IR_MARK = "/irs/hoast_o"


def viewers(ps):
    c = container_named(ps, "hoast-player")
    # A FAILED probe is not an audience of zero, and the difference matters:
    # auto_idle has a deliberate `watchers is None` branch ("uncertainty must
    # never stop the stream") that could never fire while this returned 0 for
    # both cases. sh() is the wrong helper here - its own docstring says it is
    # "for probes where empty and failed mean the same thing" - so use run()
    # and keep the ok flag.
    ok, out = run(f"docker logs {c} --since {VIEWER_WINDOW}s 2>&1") if c else (False, "")
    if not ok:
        return {"now": None, "any": None, "waiting": None,
                "window_s": VIEWER_WINDOW, "countries": {}, "ir_ips": []}
    ips, any_ips, countries, ir_ips, waiting_ips = set(), set(), {}, set(), set()
    for line in out.splitlines():
        if "[error]" in line or "[warn]" in line:
            continue
        dash = "/dash/" in line
        seg = dash and SEGMENT_MARK in line
        ir = IR_MARK in line
        if not dash and not ir:
            continue
        parts = line.split(" ", 2)
        if len(parts) < 2:
            continue
        ip, cc = parts[0], parts[1]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        # Status is the last field of the access-log line. A 404 is not
        # playback and not a renderer start, so it must not feed either. It is
        # deliberately NOT applied to "waiting" below: when the loop is down
        # the manifest can legitimately 404, and a client retrying a dead
        # manifest is the strongest evidence there is that somebody is sitting
        # on the page waiting for it to come back.
        served = line.rsplit(" ", 1)[-1].strip() in ("200", "206", "304")
        internal = (addr.is_private or addr.is_loopback
                    or addr.is_link_local or addr in CGNAT)
        if dash:
            waiting_ips.add(ip)     # loose: manifest polls count, see "waiting"
        if ir and served and not internal:
            ir_ips.add(ip)
        # Segment fetches OR served manifest polls both count as watching.
        # Until 2026-08-09 only segments counted, and the block comment below
        # explains why that was right THEN: a manifest poll against a dead
        # stream proves waiting, not watching. What changed is the Cloudflare
        # cache rule on /dash/chunk-* and /dash/init-*: segment requests are
        # now answered at the edge and never reach this log, so a
        # segments-only count collapses to roughly one viewer per PoP
        # (whoever MISSes each fresh segment first). The manifest is
        # deliberately left DYNAMIC and every player polls it every ~2 s, so
        # a SERVED manifest poll is the same per-viewer heartbeat the segment
        # rule used to be. The dead-stream case stays correct because served
        # filters out the 404s a dead manifest returns. Honest residue: a
        # paused live player keeps polling the manifest, so a parked tab now
        # counts as a viewer for as long as it sits there; pre-rule it aged
        # out after 90 s. That overcount is bounded by the number of parked
        # tabs; the undercount it replaces was unbounded.
        if not dash or not served:
            continue
        any_ips.add(ip)     # counted before the public filter: see "any" below
        if internal:
            continue
        if ip not in ips:
            ips.add(ip)
            if cc and cc not in ("--", "-", "XX"):
                countries[cc] = countries.get(cc, 0) + 1
    top = dict(sorted(countries.items(), key=lambda kv: -kv[1])[:6])
    # "now" is the audience figure the panels report, so it stays public-only.
    # "any" additionally counts LAN and VPN clients and is what the idle timer
    # reads: someone watching over Tailscale is still watching, and stopping the
    # stream under them because their address is private would be wrong.
    # "waiting" is the OLD loose rule (any /dash/ hit, manifest polls included)
    # and exists for exactly one caller: _resume_after_guest. "Is anyone
    # watching" and "is anyone waiting for this to come back" are different
    # questions, and only the first one can be answered by segment fetches.
    # When the loop is DOWN there are no segments to fetch, so a strict count
    # is structurally incapable of finding an audience there - it would report
    # zero however many people were sitting on the page. Worse, GUEST_GRACE_S
    # (120) exceeds VIEWER_WINDOW (90), so on the grace-expiry path even the
    # departing guest's viewers have aged out of the window before the check
    # runs: the loop would never resume, _resume_flag is cleared so nothing
    # retries, loop-source is filtered out of services() so no alert fires,
    # and an already-playing tab cannot re-show the start button. A manifest
    # poll against a dead stream is positive evidence of a client waiting, and
    # a false positive here is self-correcting: auto_idle uses the STRICT
    # count, so it takes the loop back down within IDLE_STOP_MIN.
    # "ir_ips" is raw and short-lived: the caller hashes it before anything is
    # persisted, so no viewer address is ever written to disk.
    return {"now": len(ips), "any": len(any_ips), "waiting": len(waiting_ips),
            "window_s": VIEWER_WINDOW, "countries": top,
            "ir_ips": sorted(ir_ips)}

def renderer_sessions(ir_ips):
    """Rolling 24 h count of clients whose binaural renderer actually started.

    Deliberately NOT the same thing as the viewer count. A viewer is anyone
    pulling segments, including someone who never unmutes; this counts the
    listeners for whom the ambisonic decode chain genuinely initialised, which
    is the number that answers "did anyone actually hear it in 3D".

    Persisted for the same reason idle_state is: a telemetry restart must not
    silently reset it. The salt is READ BACK from the same file and generated
    only when absent, so it is stable across restarts and a listener who
    returns after a reboot still dedups. That stability is exactly what gives
    the digests whatever retention exposure they have, so be precise about it:

    this is OBFUSCATION, NOT ANONYMISATION. The salt sits beside the digests,
    and IPv4 is 2^32, so anyone holding this file can recover the addresses by
    brute force in seconds. What it buys is that the file is not a greppable
    list of viewer IPs and does not become one when copied into a backup.

    The reason that is nevertheless proportionate: it discloses nothing the
    box is not already keeping in the clear. hoast-player's access log carries
    every viewer address verbatim, and the reference deployment routes
    container output to journald with 30-day retention precisely so the guest
    disclaimer's promise can be honoured. So these digests expire 29 days
    BEFORE the plaintext they were derived from. Anyone able to read this file
    can already read that log. If a deployment ever tightens journald
    retention below 24 h, this becomes the weakest link and the salt has to
    move somewhere this file is not.
    """
    try:
        st = json.loads(IRSTATE.read_text())
    except Exception:
        st = {}
    salt = st.get("_salt")
    if not salt:
        salt = os.urandom(16).hex()
        st = {"_salt": salt}
    now = time.time()
    for ip in ir_ips:
        st[hashlib.sha256((salt + ip).encode()).hexdigest()[:16]] = now
    st = {k: v for k, v in st.items()
          if k == "_salt" or (isinstance(v, (int, float)) and now - v < IR_WINDOW_S)}
    try:
        IRSTATE.write_text(json.dumps(st))
    except Exception:
        pass          # a gauge is not worth failing a collect cycle over
    return len(st) - 1        # minus the salt entry


_start_lock = threading.Lock()
_last_start = [0.0]      # epoch of the last start we issued, for the idle grace period
_last_stop  = [0.0]      # epoch of the last stop; a stop supersedes an in-flight start
_idle_cycles = [0]
_src_cache = [0.0, False]   # (checked_at, running) for live_probe
_live_since = [None]        # epoch the stream last became live, for readiness

# A player pinned to a 30 s live delay cannot start on a timeline shorter than
# that: dash.js throws "Cannot read properties of null (reading 'range')" and
# never recovers. Measured on the reference deployment, a cold start reaches fresh segments at
# t+6 s but only plays once ~35 s of history exists (first frame t+43 s). So
# liveness is not readiness, and the player must not initialise until this.
READY_S = int(os.environ.get("TEL_READY_S", "35"))

# Reachability probes, both optional and env-gated so the code stays generic
# and deployments opt in (opt in via an override; see docker-compose.override.yml.example). Neither needs a
# credential: cloudflared already serves /ready + /metrics on localhost, and
# the R2 check is an anonymous HEAD on a public object.
TUNNEL_METRICS_URL = os.environ.get("TUNNEL_METRICS_URL", "").rstrip("/")
VOD_PROBE_URL = os.environ.get("VOD_PROBE_URL", "")

# Cloudflare Web Analytics for the VOD page (page loads, not playback).
# Account-scoped GraphQL, read-only token; both env vars absent = feature
# entirely off, a deployer without Cloudflare sees no difference.
CF_ANALYTICS_TOKEN = os.environ.get("CF_ANALYTICS_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")

# Backup staleness: a marker file the PULL side stamps after each successful
# run (trusted direction only; see the deployment's backup scripts). Env
# absent = feature off. A silently dead backup defeats its purpose, so a
# stale marker joins the debounced Telegram alerts.
BACKUP_MARKER = os.environ.get("BACKUP_MARKER", "")
BACKUP_MAX_AGE_H = int(os.environ.get("BACKUP_MAX_AGE_H", "48"))


def timeline_depth():
    """Seconds of media the manifest actually advertises, summed over its
    SegmentTimeline. This, not wall-clock uptime, is the quantity a player pinned
    to a live delay needs, and unlike wall-clock tracking it survives a telemetry
    restart instead of gating an already-running stream for no reason. Gates on
    the shortest track, since the player needs both audio and video."""
    try:
        mpds = sorted(DASH.glob("*.mpd"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mpds:
            return None
        root = ET.parse(mpds[0]).getroot()
        best = None
        for st in root.iter():
            if not st.tag.endswith("SegmentTemplate"):
                continue
            ts = float(st.get("timescale") or 1) or 1.0
            total = 0.0
            for tl in st:
                if not tl.tag.endswith("SegmentTimeline"):
                    continue
                for seg in tl:
                    if seg.tag.endswith("S"):
                        total += float(seg.get("d") or 0) * (int(seg.get("r") or 0) + 1)
            if total:
                sec = total / ts
                best = sec if best is None else min(best, sec)
        return best
    except Exception:
        return None


def _note_live(is_live):
    if not is_live:
        _live_since[0] = None
    elif _live_since[0] is None:
        _live_since[0] = time.time()
    return _live_since[0]


def source_container(running_only=False):
    """Name of the loop-source container. Must look at stopped ones too: once the
    idle timer stops it, `docker ps` alone can no longer find it to start again.

    oneoff=False is load-bearing: `docker compose run ... loop-source` containers
    (the pipeline test's pusher, the guest test's pusher) carry the same service
    label, and without the filter a "stop the loop" during a guest handover
    matched the guest's own pusher and killed the publisher it was admitting.
    """
    flag = "" if running_only else "-a "
    out = sh(f'docker ps {flag}--filter "label=com.docker.compose.project={PROJECT}" '
             f'--filter "label=com.docker.compose.service={SOURCE_SVC}" '
             f'--filter "label=com.docker.compose.oneoff=False" --format "{{{{.Names}}}}"')
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return lines[0] if lines else ""


def source_start():
    """Start the loop source. Idempotent by design: that is what makes it safe to
    expose publicly, since the worst a flood achieves is the stream running, which
    is the normal state anyway. Stopping stays private."""
    with _start_lock:
        # never start the demo loop under a guest: the shared MPD tolerates
        # exactly one writer, and the guest holds the slot until it is free.
        # _start_lock also serialises this whole check-then-start against the
        # guest handover (which stops the loop under the same lock), so the
        # two can only run one-after-the-other, never interleaved.
        with _guest_lock:
            guest_busy = _guest["state"] != "free"
        if guest_busy:
            return {"ok": False, "state": "guest_active",
                    "error": "a guest session holds the stream"}
        # same rule for an owner broadcast: this endpoint is public (the
        # player's start button), and starting the loop beside a live owner
        # would be the two-writers hole all over again
        with _owner_lock:
            owner_busy = _owner["live"]
        if owner_busy:
            return {"ok": False, "state": "owner_active",
                    "error": "an owner broadcast holds the stream"}
        # coalesce a burst of start clicks, but ONLY while that start is still
        # in flight: a stop issued after it (a guest handover) supersedes it,
        # and coalescing then reports "starting" while nothing is starting -
        # which silently swallowed the resume after an operator kill
        if time.time() - _last_start[0] < 15 and _last_start[0] > _last_stop[0]:
            return {"ok": True, "state": "starting"}
        if stream_state()["publishing"]:
            return {"ok": True, "state": "already_publishing"}
        name = source_container()
        if not name:
            return {"ok": False, "state": "no_source",
                    "error": f"no {SOURCE_SVC} container in project {PROJECT}"}
        _last_start[0] = time.time()
        _idle_cycles[0] = 0
        sh(f"docker start {name}", t=30)
        print(f"source started ({name})", flush=True)
        return {"ok": True, "state": "starting"}


def source_stop(reason="manual", kill_after_s=None):
    """kill_after_s bounds docker's SIGTERM grace (-t). The guest handover
    passes a small value because it answers a held RTMP callback with a hard
    ~10 s patience, and the owner handover passes the same 3 s; the idle stop
    and /api/stop use the default (10 s), which is fine there."""
    name = source_container(running_only=True)
    if not name:
        return {"ok": True, "state": "already_stopped"}
    t_opt = f"-t {int(kill_after_s)} " if kill_after_s else ""
    sh(f"docker stop {t_opt}{name}", t=(int(kill_after_s) + 4) if kill_after_s else 40)
    _last_stop[0] = time.time()      # invalidates any in-flight start coalesce
    print(f"source stopped ({name}, {reason})", flush=True)
    return {"ok": True, "state": "stopped", "reason": reason}


def live_probe():
    """Cheap liveness, safe to poll every couple of seconds while a visitor waits
    for a cold start. Segment freshness plus one cached `docker ps` for
    source_running, no curl, mostly just a stat of the dash directory, because
    the 60 s collect cycle is far too slow to
    drive a 30 s progress indicator."""
    seg_age = segment_age()
    # source_running distinguishes "idle, press start" from "running but broken",
    # and is the one docker call here, cached so a room full of waiting browsers
    # polling every few seconds cannot turn into a docker ps storm.
    now = time.time()
    if now - _src_cache[0] > 5:
        _src_cache[0], _src_cache[1] = now, bool(source_container(running_only=True))
    live = seg_age is not None and seg_age < SEG_STALE_S
    since = _note_live(live)
    depth = timeline_depth()
    if depth is None:                       # no SegmentTimeline: fall back to wall clock
        depth = (now - since) if since else 0
    # Ceil, not round, and derived from the same number as `ready`: rounding them
    # independently let the countdown reach 0 while ready was still false, so the
    # page said "0 seconds to go" and then sat there.
    remaining = max(0.0, READY_S - depth)
    ready = live and remaining <= 0
    # Which manifest to play, as a web path. Fixed per deployment in practice
    # (earshot writes ${DASH_NAME}.mpd whoever publishes), but reporting it here
    # is what frees the player page from a baked-in stream name.
    mpd = None
    try:
        mpds = sorted(DASH.glob("*.mpd"), key=lambda p: p.stat().st_mtime, reverse=True)
        if mpds:
            mpd = "/dash/" + mpds[0].name
    except Exception:
        pass
    return {"live": live,
            # Which build is answering. Cheap to include and it is the only way
            # a user who did not clone the repo can tell what they are running,
            # which is what the bug report form has to ask for.
            "version": AMBI_VERSION,
            "ready": ready,                          # safe to initialise a player
            "timeline_s": round(depth),
            # The manifest grows one audio segment at a time, so this decreases in
            # ~5 s jumps. The page interpolates between polls; a raw display of
            # this value looks frozen for five seconds at a stretch.
            "ready_in_s": (0 if ready else int(-(-remaining // 1))) if live else None,
            "segment_age_s": seg_age,
            "source_running": _src_cache[1],
            "on_demand": IDLE_STOP_MIN > 0,
            "starting": now - _last_start[0] < 120,
            "mpd": mpd,
            # off is a state, not an absence. This endpoint is served on :8090
            # only (private), so unlike the public status.json it can say so;
            # the dashboard overlays this over stats.json and would otherwise
            # drop the row again.
            "endpoint": guest_public() or {"state": "off"}}


def idle_state(**set_):
    """Read, and optionally update, the persisted idle bookkeeping.

    Persisted rather than held in memory because the counter used to live in a
    module global: every telemetry restart silently restarted the countdown, so
    a few redeploys could keep the box encoding indefinitely with nobody
    watching, and the operator had no way to see how far along the timer was.
    """
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    if set_:
        for k, v in set_.items():
            st["_" + k] = v
        try:
            STATE.write_text(json.dumps(st))
        except Exception:
            pass
    return st.get("_last_viewer"), st.get("_idle_accum", 0.0)


def auto_idle(strm, watchers):
    """Stop the source after IDLE_STOP_MIN with nobody watching. Long hysteresis
    on purpose: a short timer plus a 30 s cold start would flap as viewers come
    and go, and each cycle costs every waiting visitor that startup wait.

    The decision runs on accumulated confirmed-idle seconds rather than wall
    clock, so a failed probe neither counts as idleness nor resets progress.
    last_viewer is tracked separately, purely so the dashboard can say how long
    it has been since anyone watched.
    """
    now = time.time()
    if watchers is not None and watchers > 0:
        idle_state(last_viewer=now, idle_accum=0.0)
        return
    if _guest["state"] != "free":
        # the publisher is a guest, not the loop; "stop the source" would be a
        # no-op on an already-stopped container, but the idle bookkeeping would
        # still churn and log for nothing. Guests are never idled out: their
        # test needs no audience, and the cap bounds the session anyway.
        return
    if IDLE_STOP_MIN <= 0 or not strm["publishing"]:
        return
    if watchers is None:
        # The viewer probe failed, so "nobody is watching" is unproven. Freeze the
        # counter rather than counting the silence as idleness: stopping the source
        # costs every visitor a 35-43 s cold start, and it would happen with no
        # alert, since stream_stalled needs publishing to be true and we would
        # have just made it false ourselves. Uncertainty must never stop the
        # stream. Not reset either, so a flapping probe cannot hold the box in a
        # permanent encode.
        return
    if now - _last_start[0] < START_GRACE_S:
        idle_state(idle_accum=0.0)
        return
    _, accum = idle_state()
    accum += INTERVAL
    if accum >= IDLE_STOP_MIN * 60:
        idle_state(idle_accum=0.0)
        source_stop("idle")
    else:
        idle_state(idle_accum=accum)


# --------------------------------------------------------------------------
# Guest test endpoint arbiter. rtmp-ingest's `guest` application sends its
# on_publish / on_publish_done / on_update callbacks here (proxied through
# ingest's http block so the hostname resolves per request). This is the only
# authority on who may publish: one guest at a time, the demo loop paused for
# the duration, a reconnect grace after a disconnect, and an absolute session
# cap enforced by answering an on_update with a non-2xx (nginx-rtmp then drops
# the publisher). No keys, no queue, first come first served, by design.
#
# These routes are reachable on TEL_PORT: host-side that is localhost/VPN
# only, and INSIDE the compose network the /rtmp/ family is gated on the HTTP
# peer address since 2026-08-09 (see do_GET): only rtmp-ingest and the two srt
# gateways get an answer, every other sibling container gets 404. Like
# /api/stop, they are trusted surface; the trust boundary is those peers plus
# whatever the operator binds 8090 to, and the public player proxies only
# /api/live, /api/start and /api/guest/report, never these.
# Master switch, OFF by default: most deployments are a single private
# publisher and should never expose a keyless application. Everything below
# no-ops when disabled, and the status surfaces omit the endpoint entirely.
GUEST_ENABLED = os.environ.get("GUEST_ENABLED", "0") == "1"
GUEST_GRACE_S = int(os.environ.get("GUEST_GRACE_S", "120"))   # reconnect window
GUEST_MAX_S   = int(os.environ.get("GUEST_MAX_S", "10800"))   # absolute cap, ±update interval
# After a session is ENDED by the cap or the operator kill (not a natural
# stop), guest publishes are refused for this long. Without it, an encoder
# with auto-reconnect re-claims the freed slot in seconds, which makes the cap
# a 3 h duty cycle instead of a limit and the kill button a two-second blip.
# 300 s only covers the dense head of OBS's default auto-reconnect schedule
# (25 tries, exponential backoff capped at 15 min, roughly 3 h total) - it
# spaces out reclaim attempts rather than outlasting the encoder's retries.
GUEST_COOLDOWN_S = int(os.environ.get("GUEST_COOLDOWN_S", "300"))
# srt-gateway trust anchor: the gateway republishes SRT sessions into the
# guest app from inside the compose network, so nginx-rtmp reports the
# gateway's container address for every SRT guest. It smuggles the CALLER's
# real address as a ?realip= publish arg; the substitution is honored only
# from the gateway's own resolved address, and this shared secret is optional
# defence in depth on top of that (see _gw_realip_ok). Empty secret =
# attribution still works, but the /gw/session/* direct-path routes fail
# closed (see _gw_session_ok).
GUEST_GW_SECRET = os.environ.get("GUEST_GW_SECRET", "")
TEL_SRT_GW_HOST = os.environ.get("TEL_SRT_GW_HOST", "srt-gateway")
# only for deciding whether srt-gateway belongs in the dashboard's service row;
# the gateway itself is the authority on whether it is actually listening.
# The "0" is the bare-process fallback for the var being absent entirely;
# compose injects SRT_ENABLED=${SRT_ENABLED:-1} here too, so under the shipped
# stack this reads True unless the operator sets 0.
SRT_ENABLED = os.environ.get("SRT_ENABLED", "0") == "1"

# Services the health path deliberately does not police, and why:
#   telemetry   is this process; if it were down nothing would be reporting
#   loop-source is STOPPED on purpose by auto-idle whenever nobody is watching,
#               so "not running" is its normal resting state, not a fault
#   shaka       is offline tooling on a compose profile, never in the live path
SERVICES_UNPOLICED = ("telemetry", "loop-source", "shaka")
INGEST_STAT   = os.environ.get("TEL_INGEST", "http://rtmp-ingest:8080/stat")
# What version is actually running. AMBI_VERSION wins so CI and a developer can
# inject an exact `git describe`; the shipped VERSION file is the fallback so a
# plain `docker compose up` still reports something truthful rather than
# nothing. Surfaced on /api/live, which the player already polls, so the
# dashboard and the public page both get it for free.
def _read_version():
    v = os.environ.get("AMBI_VERSION", "").strip()
    if v:
        return v
    try:
        return (Path("/app/VERSION").read_text().strip() or "unknown")
    except Exception:
        return "unknown"

AMBI_VERSION = _read_version()
# Max hold on the on_publish callback while the loop unwinds. nginx-rtmp's
# netcall gives up after netcall_timeout (default 10 s, an undocumented
# rtmp-level directive left at its default here), and the docker stop before
# the wait is itself bounded to ~3.5 s, so the sum must stay safely under that.
HANDOVER_S    = 4
GSTATE  = DATA/"guest_state.json"
GUESTCSV = DATA/"guest_sessions.csv"
# Abuse reports (the player's report button). Same redaction regime as the
# session log: rows persist as statistics, IP columns expire.
REPORTCSV = DATA/"guest_reports.csv"
REPORT_IP_MAX = 3            # accepted reports per reporter IP...
REPORT_IP_WINDOW_S = 1800    # ...per this window
REPORT_COOLDOWN_S = 900      # one alert per session per this window
# A stalled guest transcode (wrong audio layout, most likely stereo/mono
# OBS) is ended automatically this long after the handover if no playable
# segment has appeared. Deliberately NO publish cooldown afterwards: the
# commonest cause is an innocent misconfiguration the pusher should be able
# to fix and retry immediately.
GUEST_STALL_S = 45

# --- resource guard: a guest must not be able to cook the host --------------
# Nothing else stops a guest pushing 60 Mbps at whatever hardware this runs on.
# Two limits, deliberately asymmetric in role:
#   GUEST_MAX_TEMP_C is the SAFETY NET and the authority. It is measured on the
#   device, so it accounts for room temperature, dust, thermal soak and any
#   load the bitrate figure cannot see. 0 disables.
#   GUEST_MAX_MBPS is only a coarse PRE-FILTER that turns away the obvious
#   abuser at the door. A bitrate number alone is a bad safety net: on the
#   2012 Mac Mini deployment, a synthetic 42 Mbps ladder peaked at 87 C while a
#   real 45.5 Mbps session ran 87-96 C with ZERO viewers, a ~9 C gap that no
#   bitrate threshold could have predicted (docs/evidence/thermal-2026-08-05
#   in the deployment repo). 0 disables.
# Both default OFF so a generic deployment behaves exactly as before, and both
# are per-device: the right numbers come from measuring the host, not from code.
GUEST_MAX_TEMP_C = int(os.environ.get("GUEST_MAX_TEMP_C", "0"))
GUEST_MAX_MBPS   = float(os.environ.get("GUEST_MAX_MBPS", "0"))
# Sustained, not instantaneous: one hot sample or one bitrate spike during a
# keyframe must not end a session. Both limits must hold for this many
# consecutive update pings (~10 s apart) before the session is dropped.
GUEST_LIMIT_STRIKES = int(os.environ.get("GUEST_LIMIT_STRIKES", "3"))
_guest_strikes = {"temp": 0, "rate": 0}

_guest_lock = threading.Lock()
# `direct` and `session` carry the DIRECT path (guest-direct-dash design).
# A direct guest reuses this whole state machine - admission, cap, kick,
# grace, cooldown, bans, the dashboard - and differs in exactly two ways:
# there is no RTMP publisher behind it, so every stat-based probe must skip
# it (guard on `direct`), and its liveness arrives as gateway beats keyed on
# `session` rather than as nginx on_update pings.
_guest = {"state": "free", "name": None, "addr": None, "start": None,
          "last_seen": None, "grace_started": None, "kill": False,
          "terminating": None, "cooldown_until": None,
          "reports": 0, "last_report_alert": None,
          "last_end": None, "last_end_reason": None,
          "direct": False, "session": None}
_reporters = {}              # reporter ip -> [accepted-report epochs]
_stall_timer = [None]
# viewer attribution for the CURRENT guest session (guest sessions ARE the
# live stream while they run, so this is temporal attribution of the same
# counter, not a second one). Guarded by _guest_lock.
_guest_view = {"peak": 0, "sum": 0, "n": 0}
_guest_timer = [None]      # the pending grace-expiry threading.Timer
_resume_flag = [False]     # a resume attempt is owed (retried from guest_tick)
_pub_cache = [None]        # last status.json dict, for out-of-cycle endpoint updates

# --- owner-live latch --------------------------------------------------
# Set when an EXTERNAL owner (a /owner publish whose name is not the demo
# loop's) goes live; cleared by its publish_done or by the owner_tick
# backstop. While set: guests are refused for the WHOLE owner session (not
# just preempted at takeover), and neither a visitor's start button nor a
# guest-end resume may put the demo loop back beside the owner.
# The discriminator is the loop's PUBLISH NAME, which is DASH_NAME - the
# loop publishes /owner/${DASH_NAME:-hoast_demo}?token=<key>, so its name is
# never the key (comparing against LOOP_SOURCE_KEY was the review's blocker: on
# any box where the key is a real secret, the loop's own publish would read
# as an owner and stop itself in a start/stop flap). This also means the
# key itself never needs to reach this container. An owner must simply not
# publish UNDER the loop's name (earshot would refuse the same-name
# duplicate anyway); the owner gateway's streamid and the documented
# name-as-key RTMP form both satisfy that on their own.
# An older nginx template forwards no name at all: every /rtmp/owner/notify
# then falls back to the legacy preempt-at-takeover behavior and the latch
# never engages - safe in either rollout order.
# The latch is memory-only, but it does not stay lost: after a telemetry
# restart mid-owner-broadcast an RTMP owner is re-derived from ingest's own
# publisher list on the next collect cycle (_owner_relatch_check), and a
# DIRECT owner's next beat is answered 410, so the gateway re-claims and
# re-arms it within one beat. Until that lands, an RTMP owner still keeps
# guests out through the handover's _earshot_unwound check (earshot never
# goes quiet under a live owner), just with a clumsier 503, and the loop
# stays down because source_start sees "already_publishing" - neither of
# which covers a DIRECT owner, whose writer reaches no stat page.
LOOP_NAME = os.environ.get("DASH_NAME", "hoast_demo")
_owner_lock = threading.Lock()
_owner = {"live": False, "name": None, "since": None, "session": None}
_owner_miss = [0]          # consecutive owner_tick probes with no publisher
OWNER_END_REASON = "ended for an owner broadcast"   # NOT in _guest_end_locked's
                           # cooldown list on purpose: a guest preempted for an
                           # owner is innocent, so no 300 s cooldown afterwards


def _guest_sanitize(name):
    return (re.sub(r"[^A-Za-z0-9_-]", "", name or "")[:32]) or "guest"


# The DIRECT-path trust anchor (guest-direct-dash design, BLOCKER 1 and
# BLOCKER-owner). Deliberately NOT _gw_realip_ok: that one checks an address
# carried in a QUERY FIELD, which is unforgeable only because nginx-rtmp fills
# it in from the real publisher socket. The /gw/ routes have no nginx in front
# - the gateways call telemetry directly - so the only trustworthy identity
# there is the HTTP peer address of the connection itself, which is what
# _gw_peer_role() checks and what the routes must use.
#
# Two hosts, because there are two gateway containers and they are NOT
# interchangeable: srt-gateway admits arbitrary guests, srt-gateway-owner
# carries the owner's credentials. A single anchor would let a compromised
# guest gateway claim owner sessions.
TEL_SRT_GW_OWNER_HOST = os.environ.get("TEL_SRT_GW_OWNER_HOST",
                                       "srt-gateway-owner")
# rtmp-ingest is the only legitimate caller of the /rtmp/* callback routes.
# They are fail-open by contract (nginx masks failures so a telemetry blip
# cannot kill a broadcast), which is right for a callback from a trusted
# neighbour and wrong for anything else: unauthenticated, ANY compose-network
# peer could latch the owner state with /rtmp/owner/notify?name=x, which locks
# every guest out and holds the demo loop down until the tick backstop
# notices. The review classified this alias as permanent infrastructure - the
# demo loop and the still-supported RTMP owner route both fire it - so it is
# authenticated rather than retired.
TEL_INGEST_HOST = os.environ.get("TEL_INGEST_HOST", "rtmp-ingest")
_gw_role_cache = [0.0, {}]


def _gw_peer_role(peer, force=False):
    """Which gateway, if any, is this HTTP peer address? Returns 'guest',
    'owner' or None.

    Resolved through docker's DNS with the same short cache _gw_ip uses, and
    re-resolved once on a miss, so a recreated gateway (new container IP) is
    picked up immediately rather than being refused for up to the cache TTL.
    srt-gateway-owner lives only in the deployment override and is absent on a
    default install; an unresolvable name simply matches nothing."""
    if not peer:
        return None
    now = time.time()
    if force or now - _gw_role_cache[0] > 5:
        table = {}
        for role, host in (("guest", TEL_SRT_GW_HOST),
                           ("owner", TEL_SRT_GW_OWNER_HOST)):
            try:
                table[socket.gethostbyname(host)] = role
            except OSError:
                pass
        _gw_role_cache[0], _gw_role_cache[1] = now, table
    role = _gw_role_cache[1].get(peer)
    if role is None and not force:
        return _gw_peer_role(peer, force=True)
    return role


_gw_rate = {}
_gw_rate_lock = threading.Lock()
GW_RATE_MIN_GAP_S = 1.0     # a beat every 10 s, a claim retry every 2 s


def _gw_rate_ok(peer):
    """Per-peer floor on /gw/ call spacing.

    The 2026-08-09 incident is the specification here: a keepalive whose
    cadence was accidentally event-driven called telemetry ~80 times a second,
    each call taking a thread from an unbounded ThreadingTCPServer, until the
    container hit its pids limit and the arbiter died - repeatedly. Wall-clock
    cadence on the client fixes the honest case; this fixes the dishonest and
    the buggy ones, and makes that whole class of regression locally testable
    without needing SRT at all. The floor is far below any legitimate rate, so
    a well-behaved gateway never sees a 429."""
    now = time.time()
    with _gw_rate_lock:
        last = _gw_rate.get(peer, 0.0)
        if now - last < GW_RATE_MIN_GAP_S:
            return False
        _gw_rate[peer] = now
        if len(_gw_rate) > 64:              # bounded: only gateways reach here
            for k in [k for k, v in _gw_rate.items() if now - v > 300]:
                _gw_rate.pop(k, None)
    return True


def _ingest_peer_ok(peer):
    """Is this HTTP peer rtmp-ingest itself?

    Same socket-bound reasoning as _gw_peer_role: the address of the
    connection is the one identity its own claimant cannot forge, and
    rtmp-ingest runs cap_drop:[ALL] like everything else here, so it cannot
    spoof a neighbour either. Loopback is allowed because the operator's own
    curl from inside the telemetry container is a legitimate debugging path
    and cannot come from the network."""
    if peer in ("127.0.0.1", "::1"):
        return True
    now = time.time()
    if now - _ingest_ip_cache[0] > 5:
        try:
            _ingest_ip_cache[1] = socket.gethostbyname(TEL_INGEST_HOST)
        except OSError:
            _ingest_ip_cache[1] = None
        _ingest_ip_cache[0] = now
    if peer == _ingest_ip_cache[1]:
        return True
    # one forced re-resolve: a recreated ingest changes IP, and a stale cache
    # must not start refusing real callbacks
    try:
        fresh = socket.gethostbyname(TEL_INGEST_HOST)
    except OSError:
        return False
    _ingest_ip_cache[0], _ingest_ip_cache[1] = now, fresh
    return peer == fresh


_ingest_ip_cache = [0.0, None]


def _gw_session_ok(peer, token):
    """Authenticate a /gw/session/* call: the peer must BE one of the gateway
    containers, and the shared secret must match.

    The secret is MANDATORY here, unlike on the RTMP path where it is optional
    defence-in-depth. Without it any compose-network peer that could reach
    telemetry could self-admit, kick a live session, or frame an arbitrary IP
    for a ban; the address check alone would still be sound, but the two
    together mean an attacker needs both a specific container identity AND a
    secret. Returns the role on success, None otherwise."""
    if not GUEST_GW_SECRET:
        return None
    if not (token and hmac.compare_digest(token, GUEST_GW_SECRET)):
        return None
    return _gw_peer_role(peer)


_gw_ip_cache = [0.0, None]


def _gw_ip(force=False):
    """srt-gateway's current container address, re-resolved through docker's
    DNS with a short cache so a recreated gateway is picked up within 5 s.
    force re-resolves now: used to bust a stale entry rather than misattribute
    a real guest to the gateway's own (old) IP right after a gateway restart."""
    now = time.time()
    if force or now - _gw_ip_cache[0] > 5:
        try:
            _gw_ip_cache[1] = socket.gethostbyname(TEL_SRT_GW_HOST)
        except OSError:
            _gw_ip_cache[1] = None
        _gw_ip_cache[0] = now
    return _gw_ip_cache[1]


def _gw_realip_ok(reporter, token, realip):
    """Honor a ?realip= claim only from the gateway itself.

    The load-bearing check is the ADDRESS one: `reporter` is nginx-rtmp's own
    addr=, taken from the TCP connection, and a publisher cannot forge it
    (nginx emits addr= before appending the publisher's own publish-URL args,
    and parse_qs returns the first occurrence). So a remote guest appending
    ?realip= to its URL is refused because its address is its own, not the
    gateway's - which is what keeps attribution, and therefore bans, honest.

    GUEST_GW_SECRET is optional defence-in-depth on top of that: set it and a
    matching token is also required, which additionally distinguishes the
    gateway from any other container that might come to share its address.
    Unset, attribution still works and the endpoint is usable out of the box."""
    if not realip:
        return False
    if GUEST_GW_SECRET and not (token and hmac.compare_digest(token, GUEST_GW_SECRET)):
        return False
    # a mismatch is usually a stale cache (the gateway was just recreated)
    # rather than an impostor, so re-resolve once before refusing - otherwise
    # that guest gets logged, and bannable, as the gateway's own address
    if reporter != _gw_ip() and reporter != _gw_ip(force=True):
        return False
    try:
        ipaddress.ip_address(realip)
    except ValueError:
        return False
    return True


def _guest_save():
    try:
        GSTATE.write_text(json.dumps(_guest))
    except Exception:
        pass


# The public notice says connection details are retained for 30 days. The
# session log follows the same split the viewer stats always used (counts and
# countries persist, identifiers do not): rows are kept indefinitely for
# statistics, but the IP column is REDACTED once it ages past the window. The
# country, resolved at write time and kept, is the aggregate-level residue,
# exactly like viewers.csv's country codes. Container stdout logs rotate by
# size, not by days, so they are a retention residual.
GUEST_RETENTION_S = int(os.environ.get("GUEST_RETENTION_DAYS", "30")) * 86400
# IP bans (dashboard "End + ban"). Clamped to the retention window on
# purpose: expiry and IP redaction are ONE event, so a ban outliving the
# address that defines it would be incoherent.
BANSCSV = DATA/"guest_bans.csv"
GUEST_BAN_DAYS = min(int(os.environ.get("GUEST_BAN_DAYS", "30")),
                     GUEST_RETENTION_S // 86400)

# --- offline IP -> country, for the session statistics ---------------------
# DB-IP's free country-lite CSV (CC BY 4.0; attribution in the dashboard
# footer and telemetry/README.md), fetched ONCE into the data volume at boot,
# parsed with the stdlib only (the image deliberately carries no extra python
# deps). Everything fails soft to "--": geolocation is a statistics nicety,
# never worth blocking on, and guest IPs are deliberately NEVER sent to any
# online lookup service (that would hand a third party the very data the
# notice promises to guard).
GEOCSV = DATA/"dbip-country-lite.csv.gz"
_geo = {"v4": [], "v6": [], "loaded": False}


def _geo_load():
    import gzip, csv as _csv, ipaddress, bisect
    if not GEOCSV.exists():
        # try current, then previous month (db-ip republishes monthly)
        from datetime import date
        months = []
        y, m = date.today().year, date.today().month
        months.append(f"{y}-{m:02d}")
        y2, m2 = (y, m - 1) if m > 1 else (y - 1, 12)
        months.append(f"{y2}-{m2:02d}")
        for mo in months:
            url = f"https://download.db-ip.com/free/dbip-country-lite-{mo}.csv.gz"
            ok, _ = run(f"curl -fsSL --max-time 60 -o {GEOCSV}.tmp {url}", t=90)
            if ok:
                Path(f"{GEOCSV}.tmp").replace(GEOCSV)
                print(f"geoip: fetched dbip-country-lite {mo}", flush=True)
                break
            Path(f"{GEOCSV}.tmp").unlink(missing_ok=True)
    if not GEOCSV.exists():
        print("geoip: no database (offline?); guest countries will read --", flush=True)
        return
    try:
        v4, v6 = [], []
        with gzip.open(GEOCSV, "rt") as f:
            for row in _csv.reader(f):
                if len(row) < 3:
                    continue
                try:
                    a, b = ipaddress.ip_address(row[0]), ipaddress.ip_address(row[1])
                except ValueError:
                    continue
                (v4 if a.version == 4 else v6).append((int(a), int(b), row[2]))
        v4.sort(); v6.sort()
        _geo["v4"], _geo["v6"], _geo["loaded"] = v4, v6, True
        print(f"geoip: loaded {len(v4)} v4 + {len(v6)} v6 ranges", flush=True)
    except Exception as e:
        print("geoip: load failed:", e, flush=True)


def geo_cc(ip):
    """Country code for an address, '--' when unknown. Pure stdlib bisect."""
    try:
        import ipaddress, bisect
        a = ipaddress.ip_address(ip)
        table = _geo["v4"] if a.version == 4 else _geo["v6"]
        if not table:
            return "--"
        i = bisect.bisect_right(table, (int(a), 2**129, "")) - 1
        if i >= 0 and table[i][0] <= int(a) <= table[i][1]:
            return table[i][2]
        return "--"
    except Exception:
        return "--"


def anon_ip(ip):
    """Truncate, do not erase: keep the network part so long-term statistics
    can still see repeat networks and ban-evasion patterns (the player disclaimer states the same rule). v4 keeps /24 (a.b.c.x), v6
    keeps /48 (x:x:x::x). Idempotent; rows already fully redacted to "-"
    stay "-" (that data is gone). Truncated values can never equal a real
    address, so ban enforcement stays inert on them by construction."""
    if not ip or ip in ("-", "") or ip.endswith(".x") or ip.endswith("::x"):
        return ip
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "-"
    if addr.version == 4:
        return ".".join(ip.split(".")[:3]) + ".x"
    hexts = addr.exploded.split(":")[:3]
    return ":".join(h.lstrip("0") or "0" for h in hexts) + "::x"


def _redact_csv(path, ip_cols):
    """Shared redactor: IP columns expire past the retention window, rows
    stay as anonymised statistics."""
    try:
        if not path.exists():
            return
        cutoff = time.time() - GUEST_RETENTION_S
        rows = path.read_text().splitlines()
        out, changed = [], 0
        for r in rows:
            p = r.split(",")
            try:
                old = datetime.fromisoformat(p[0]).timestamp() < cutoff
            except Exception:
                old = False
            if old:
                for c in ip_cols:
                    if len(p) > c and p[c] not in ("-", "") and not p[c].endswith((".x", "::x")):
                        p[c] = anon_ip(p[c]); changed += 1
            out.append(",".join(p))
        if changed:
            tmp = path.with_suffix(".csv.tmp")
            tmp.write_text("\n".join(out) + "\n")
            tmp.replace(path)
            print(f"{path.name}: redacted {changed} IP field(s) past retention", flush=True)
    except Exception:
        pass


def _guest_log_expire_ips():
    """Redact the IP column of rows older than the retention window; the rows
    themselves stay forever as anonymised statistics (timestamp, name,
    country, duration, end reason). Cheap: a few KB once per collect cycle."""
    _redact_csv(REPORTCSV, [2, 4])     # publisher ip, reporter ip
    try:
        if not GUESTCSV.exists():
            return
        cutoff = time.time() - GUEST_RETENTION_S
        rows = GUESTCSV.read_text().splitlines()
        out, changed = [], 0
        for r in rows:
            p = r.split(",")
            # rows are ts,name,addr,cc,dur,reason,peak,mean (legacy rows lack
            # cc, and rows written before viewer attribution lack peak/mean)
            if len(p) >= 5 and p[2] not in ("-", "") and not p[2].endswith((".x", "::x")):
                try:
                    if datetime.fromisoformat(p[0]).timestamp() < cutoff:
                        p[2] = anon_ip(p[2])
                        changed += 1
                except Exception:
                    pass            # unparseable timestamp: leave untouched
            out.append(",".join(p))
        if changed:
            tmp = GUESTCSV.with_suffix(".csv.tmp")
            tmp.write_text("\n".join(out) + "\n")
            tmp.replace(GUESTCSV)
            print(f"guest log: redacted {changed} IP(s) past retention", flush=True)
    except Exception:
        pass


def _guest_log(reason):
    """One CSV row per finished session: contention and abuse stay visible.
    Country is resolved locally at write time (see _geo_load); the IP column
    expires after GUEST_RETENTION_DAYS (see _guest_log_expire_ips). Trailing
    columns: peak and mean public viewer count sampled while the session was
    live, so "did anyone actually watch this" is answerable per session
    (aggregate counts, no new personal data)."""
    try:
        start = _guest.get("start")
        dur = round(time.time() - start) if start else 0
        cc = geo_cc(_guest.get("addr") or "")
        peak = _guest_view["peak"]
        mean = round(_guest_view["sum"] / _guest_view["n"], 1) if _guest_view["n"] else 0
        with open(GUESTCSV, "a") as f:
            f.write(f"{now_iso()},{_guest.get('name')},{_guest.get('addr')},{cc},{dur},{reason},{peak},{mean}\n")
    except Exception:
        pass


def guest_public():
    """The publicly shown slice: no addr. remaining_s is cap time while live,
    reconnect window while in grace. None when the feature is disabled, so
    every status surface simply omits it."""
    if not GUEST_ENABLED:
        return None
    with _guest_lock:
        st, now = dict(_guest), time.time()
    out = {"state": st["state"], "name": st["name"], "grace_s": GUEST_GRACE_S,
           "remaining_s": None,
           # the disclaimer's numeric claims interpolate from these, so the
           # prose can never drift from the running config
           "max_h": round(GUEST_MAX_S / 3600, 1),
           "retention_days": GUEST_RETENTION_S // 86400,
           "ban_days": GUEST_BAN_DAYS}
    if st["state"] == "live" and st["start"]:
        out["remaining_s"] = max(0, round(GUEST_MAX_S - (now - st["start"])))
        out["viewers_peak"] = _guest_view["peak"]
    elif st["state"] == "grace" and st["grace_started"]:
        out["remaining_s"] = max(0, round(GUEST_GRACE_S - (now - st["grace_started"])))
    cd = st.get("cooldown_until")
    if st["state"] == "free" and cd and now < cd:
        out["cooldown_s"] = round(cd - now)
    # surfaced for 10 minutes so the pusher can see WHY their session ended
    # (OBS reports a successful connection either way, so without this the
    # commonest failure, wrong audio layout, is also the most confusing)
    if st.get("last_end") and now - st["last_end"] < 600 and st.get("last_end_reason"):
        out["last_end_reason"] = st["last_end_reason"]
    # guest_publish() 403s every guest for the whole owner session (the
    # owner-live latch), but until now that never showed here: a would-be
    # guest reading "free" had no way to know a push would be rejected.
    # Only override when the guest slot is otherwise idle - an ACTIVE guest
    # session already reports its own state, and ends the moment the owner
    # takes over anyway. Not nested under _guest_lock (already released
    # above): guest_public() is only ever called from outside both locks
    # (see the no-cross-lock-calls invariant on guest_publish's handover
    # section), so acquiring _owner_lock here on its own is safe.
    if st["state"] == "free":
        with _owner_lock:
            owner_live = _owner["live"]
        if owner_live:
            out["state"] = "owner"
            out.pop("cooldown_s", None)
    return out


def _refresh_pub_endpoint():
    """Push the new endpoint state into status.json between collect cycles: the
    player badge must not lag a takeover by up to a minute."""
    pub = _pub_cache[0]
    ep = guest_public()
    if not pub or not ep:
        return
    try:
        pub = dict(pub); pub["endpoint"] = ep
        PUB.write_text(json.dumps(pub))
    except Exception:
        pass


def _grace_timer_arm(seconds):
    if _guest_timer[0]:
        _guest_timer[0].cancel()
    t = threading.Timer(max(0.5, seconds), guest_tick)
    t.daemon = True
    t.start()
    _guest_timer[0] = t


def _guest_end_locked(reason):
    """Caller holds _guest_lock. Returns to free and logs; resuming the loop is
    the caller's job AFTER releasing the lock (source_start probes docker)."""
    _guest_log(reason)
    name = _guest.get("name")
    start = _guest.get("start")
    dur = round(time.time() - start) if start else 0
    if _guest_timer[0]:
        _guest_timer[0].cancel(); _guest_timer[0] = None
    # forced endings arm the cooldown; a natural stop (grace expiry) does
    # not, and neither does a stalled transcode: that is usually an innocent
    # misconfiguration whose fix should be retryable immediately
    cooldown = (time.time() + GUEST_COOLDOWN_S
                if reason in ("session cap", "operator kill") and GUEST_COOLDOWN_S > 0
                else None)
    if _stall_timer[0]:
        _stall_timer[0].cancel(); _stall_timer[0] = None
    # strikes belong to the session that earned them: a guest ended for heat
    # must not hand its counters to the innocent next one
    _guest_strikes["temp"] = _guest_strikes["rate"] = 0
    # direct/session cleared with everything else: a freed slot must not keep
    # claiming to be a direct writer (stream_state reads that flag) and the
    # retired session id must never match a late beat from the gateway whose
    # session this was - that beat gets 410 and a clean re-claim instead.
    # A direct session just ended. Unless the gateway told us its writer had
    # already exited (gw_done clears this immediately after), assume it may
    # still be draining and fence it before anything else can start writing.
    if _guest["direct"]:
        _direct_fence[0] = True
    _guest.update(state="free", name=None, addr=None, start=None,
                  last_seen=None, grace_started=None, kill=False,
                  terminating=None, cooldown_until=cooldown,
                  reports=0, last_report_alert=None, direct=False, session=None,
                  last_end=time.time(), last_end_reason=reason)
    _guest_save()
    _resume_flag[0] = True
    print(f"guest session ended ({reason})"
          + (f"; cooldown {GUEST_COOLDOWN_S}s" if cooldown else ""), flush=True)
    # ENDED ALERT, only for the two reasons the operator could not already know
    # about. "operator kill" and the owner-preempt reason are the operator's
    # own action (they clicked End, or they just went live themselves); the
    # stall reason already sent its own telegram() the moment it was detected,
    # in _guest_stall_arm, so alerting again here would double-send for that
    # one session. That leaves a natural stop (nobody reconnected within grace)
    # and hitting the absolute session cap, both of which happen unattended.
    if reason == "grace expired":
        telegram(f"guest stream '{name}' ended after {dur_short(dur)}")
    elif reason == "session cap":
        telegram(f"guest stream '{name}' ended after {dur_short(dur)}: "
                 f"hit the {round(GUEST_MAX_S / 3600, 1)}h session limit")


_direct_fence = [False]     # a direct session ended; its writer may still live


def _fence_direct_writers():
    """Force any direct writer in earshot to stop, without needing the
    gateway to cooperate.

    The beat-403 kick is COOPERATIVE: it only works if the gateway is alive,
    reachable and honest. The case that breaks it is mundane rather than
    malicious - a network partition between the gateway and telemetry, where
    beats fail (correctly treated as transient, so the broadcast survives a
    blip) while the gateway keeps feeding earshot perfectly well. Telemetry
    meanwhile demotes the session on beat silence and eventually resumes the
    demo loop, and that is two writers on the single-writer DASH tree with
    nothing left to end either.

    Telemetry holds the docker socket, so it has a lever the gateway cannot
    veto: kill the socat that owns the listener socket. The transcoder behind
    it then sees EOF and exits cleanly, and the entrypoint re-arms the port
    within about a second - the same mechanism the CLOSE_WAIT watchdog inside
    earshot already uses. Safe to call when nothing is connected: it kills an
    idle listener, which immediately comes back."""
    c = container_named(docker_ps(), "earshot")
    if not c:
        return
    # Every listener earshot arms, or the fence leaves a writer standing. The
    # set grew from two to four when the port started carrying the video codec
    # as well as the track count (9100/9102 4x4, 9101/9103 1x4).
    for port in (9100, 9101, 9102, 9103):
        sh(f'docker exec {c} pkill -9 -f "socat -u TCP-LISTEN:{port}"', t=8)
    print("fenced direct writers (listener sockets recycled)", flush=True)


def _resume_after_guest():
    """Hand control back to the loop's NORMAL on-demand rule, evaluated now:
    with idling disabled the loop simply runs; with idling enabled it returns
    only if somebody is actually watching, otherwise the visitor flow starts it
    later, exactly as if the guest had never existed."""
    if _guest["state"] != "free":
        return
    # A guest killed FOR an owner takeover reaches here via its own
    # publish_done while the owner is live: resuming the loop now would put
    # it beside the owner on one MPD. The resume is not owed to anyone - the
    # owner's publish_done (or the owner_tick backstop) re-runs this when the
    # slot is genuinely free - so the flag is cleared, not left for the
    # guest_tick retry.
    # NOTE the lock is released before the refresh: _refresh_pub_endpoint
    # takes _guest_lock (via guest_public), and guest_publish's flip re-check
    # nests _guest_lock -> _owner_lock, so calling it under _owner_lock here
    # would be the AB-BA deadlock the round-3 review caught
    with _owner_lock:
        owner_live = _owner["live"]
    if owner_live:
        _resume_flag[0] = False
        _refresh_pub_endpoint()         # the guest's end must not sit unshown
        return                          # for a full collect cycle
    # A cap/kill ends the session the instant on_publish_done arrives, which
    # can be BEFORE the dropped guest's relay finishes tearing down at
    # earshot. source_start would then see "publishing", no-op, and report
    # success, leaving the loop down with nothing left to retry (the exact
    # T4 failure in testing). Grace-expiry resumes never wait here: the
    # teardown finished long before the window closed.
    # Before the loop may start, make sure no direct writer is still alive.
    # _earshot_unwound below cannot see one - it reads earshot's RTMP stat,
    # and a direct session appears in no stat page - so this is the only
    # thing standing between a resumed loop and a second writer.
    if _direct_fence[0]:
        _direct_fence[0] = False
        _fence_direct_writers()
    _earshot_unwound(6)
    if IDLE_STOP_MIN <= 0:
        r = source_start()
    else:
        vw = viewers(docker_ps())
        # "waiting", NOT "any": nothing is publishing at this point, so no
        # client can be fetching segments and the strict count is always 0
        # here by construction. See the note on viewers()'s return value.
        # None means the probe failed: start rather than leave the demo dark
        # on a guess, matching auto_idle's rule that uncertainty must never be
        # what takes the stream down. An unnecessary start costs one idle
        # cycle; a wrong stop costs every waiting visitor a cold start.
        w = vw.get("waiting")
        if w is None or w > 0:
            r = source_start()
        else:
            idle_state(idle_accum=0.0)
            _resume_flag[0] = False
            print("guest slot free; loop stays idle (no viewers)", flush=True)
            _refresh_pub_endpoint()
            return
    # "already_publishing" here is the teardown race, not success: keep the
    # resume owed so the guest_tick backstop retries it.
    if r.get("ok") and r.get("state") != "already_publishing":
        _resume_flag[0] = False
    _refresh_pub_endpoint()


def guest_tick():
    """Timer target and per-cycle backstop: expire grace, catch a dead session
    whose on_update stopped arriving, retry an owed loop resume."""
    if not GUEST_ENABLED:
        return
    ended = None
    with _guest_lock:
        now = time.time()
        if _guest["state"] == "grace" and _guest["grace_started"]:
            if now - _guest["grace_started"] >= GUEST_GRACE_S:
                ended = "grace expired"
                _guest_end_locked(ended)
        elif _guest["state"] == "live":
            # update pings come every 10 s; 60 s of silence means ingest died or
            # the callback path broke mid-session. Treat as a disconnect.
            if _guest["last_seen"] and now - _guest["last_seen"] > 60:
                _guest.update(state="grace", grace_started=now)
                _guest_save()
                _grace_timer_arm(GUEST_GRACE_S)
                print("guest updates stopped; entering grace", flush=True)
        elif _guest["state"] == "handover":
            # a handover is seconds long; a slot stuck here means both the
            # flip and the publisher's done notification were lost
            if _guest["last_seen"] and now - _guest["last_seen"] > 30:
                _guest.update(state="grace", grace_started=now)
                _guest_save()
                _grace_timer_arm(GUEST_GRACE_S)
                print("stale handover; entering grace", flush=True)
    if ended or _resume_flag[0]:
        _resume_after_guest()


def _ingest_guest_publishing():
    """Does ingest's stat page show a publisher on the guest application?"""
    x = sh(f"curl -s --max-time 4 {INGEST_STAT}")
    seg = x.split("<name>guest</name>", 1)
    return len(seg) == 2 and "<publishing/>" in seg[1].split("</application>", 1)[0]


def _earshot_unwound(deadline_s):
    """Wait for earshot to have no publisher (the loop's relay fully gone) so
    the guest's exec transcoder never runs beside the loop's on one MPD."""
    end = time.time() + deadline_s
    while time.time() < end:
        if not stream_state()["publishing"]:
            return True
        time.sleep(0.3)
    return False


def _handover_settled(deadline_s):
    """Both halves of "the loop is gone", sharing ONE budget. Returns
    (settled, reason) so a refusal can say which half failed.

    The container half used to be a single shot taken the instant `docker stop`
    returned, while only the earshot half was allowed to wait. That is not
    symmetric, and the asymmetry is a real race: `docker stop` returning means
    the daemon has reaped the process, not that `docker ps` has stopped listing
    the container, and the gap widens on a loaded host. When earshot was already
    unwound - the common case, since the loop's relay usually drops first - the
    earshot half returned on its first iteration and the container half fired
    microseconds later, so a handover could be refused with essentially its
    whole budget unspent. CI failed 2 of 10 cycles that way (2026-08-12),
    reporting "timed out after 0.1s" against a 7.5 s budget, which is not a
    timeout and sent the diagnosis toward slow runners for a day.
    """
    end = time.time() + deadline_s
    if not _earshot_unwound(deadline_s):
        return False, "earshot still had a publisher"
    while True:
        still = source_container(running_only=True)
        if not still:
            return True, ""
        if time.time() >= end:
            return False, f"loop container {still} still running"
        time.sleep(0.2)


def guest_publish(name, addr):
    """on_publish for the guest app. 2xx accepts; anything else rejects."""
    if not GUEST_ENABLED:
        return 403
    # ban check first: blocks only rows that are active AND still carry an
    # IP AND are unexpired by the clock, all three explicit, so a stale
    # label after a missed sweep can never wrongly block anyone
    if _ban_blocks(addr):
        print(f"guest publish rejected (banned): {addr}", flush=True)
        return 403
    # the owner-live latch: guests stay out for the whole owner session, not
    # just at takeover (without this, a guest arriving two minutes into an
    # owner broadcast would claim the slot and race the owner at earshot)
    with _owner_lock:
        if _owner["live"]:
            print(f"guest publish rejected (owner live): {name} from {addr}",
                  flush=True)
            return 403
    name = _guest_sanitize(name)
    with _guest_lock:
        if _guest["state"] in ("live", "handover"):
            print(f"guest publish rejected (busy): {name} from {addr}", flush=True)
            return 403
        cd = _guest.get("cooldown_until")
        if cd and time.time() < cd:
            print(f"guest publish rejected (cooldown {round(cd - time.time())}s left): "
                  f"{name} from {addr}", flush=True)
            return 403
        resumed_session = _guest["state"] == "grace"
        # a grace window belongs to the caller who opened the session: a
        # different address arriving during it must not inherit the session
        # (and its clock, and any pending kill) - it waits for grace to end
        if resumed_session and _guest["addr"] and addr != _guest["addr"]:
            print(f"guest publish rejected (grace held by {_guest['addr']}): "
                  f"{name} from {addr}", flush=True)
            return 403
        if not resumed_session:
            _guest_view.update(peak=0, sum=0, n=0)
        # claim the slot as "handover" before the slow unwind: a concurrent
        # second publish is rejected rather than racing us, and the status
        # pages can show "switching over" instead of pretending it is live.
        # A grace reconnect keeps the session clock AND any pending operator
        # kill: disconnecting must not launder a kill away.
        start = _guest["start"] if resumed_session and _guest["start"] else time.time()
        keep_kill = _guest["kill"] if resumed_session else False
        # a carried kill must keep its carried REASON too: an owner-preempt
        # kill that rode a handover-timeout grace into this reconnect would
        # otherwise end as "operator kill" at the first update ping and arm
        # the 300 s cooldown the owner-preempt reason deliberately avoids
        keep_term = _guest["terminating"] if resumed_session and keep_kill else None
        if _guest_timer[0]:
            _guest_timer[0].cancel(); _guest_timer[0] = None
        _guest.update(state="handover", name=name, addr=addr, start=start,
                      last_seen=time.time(), grace_started=None, kill=keep_kill,
                      terminating=keep_term, cooldown_until=None)
        _guest_save()
    # Loop handover, serialised against source_start via _start_lock so a
    # visitor's start cannot interleave with the stop and connect the loop
    # underneath a freshly admitted guest. The stop is UNCONDITIONAL: a loop
    # container that was just started but whose ffmpeg has not yet reached
    # earshot is invisible to the stat probe, and skipping the stop for it was
    # exactly the two-writers hole. The whole hold stays under nginx-rtmp's
    # ~10 s netcall patience: docker stop -t 3 (<=3.5 s) + unwind (<=4 s).
    # Timed, not just bounded. HANDOVER_S is tuned for the hosts this has run
    # on so far, and a slower one simply misses it: a 2-core CI runner blew the
    # budget on 2 of 10 cycles (2026-08-10), which matters because the Pi 4 is
    # a target and may well be slower again. Logging the ELAPSED time on both
    # paths turns "it timed out" into a number, so the budget can be set from
    # measurement on the slowest real host rather than from the fastest one.
    _hand_t0 = time.time()
    with _start_lock:
        source_stop("guest handover", kill_after_s=3)
        settled, why = _handover_settled(HANDOVER_S)
    _hand_s = time.time() - _hand_t0
    if not settled:
        # could not clear the slot in time: refuse this publish but leave the
        # slot in grace with the loop already stopping, so an immediate manual
        # retry succeeds and an abandoned attempt still resumes the loop.
        with _guest_lock:
            _guest.update(state="grace", name=None, addr=None, start=None,
                          grace_started=time.time())
            _guest_save()
            _grace_timer_arm(GUEST_GRACE_S)
        print(f"guest handover gave up after {_hand_s:.1f}s: {why} "
              f"(budget: docker stop <=3.5s + unwind {HANDOVER_S}s); "
              f"slot in grace ({name} from {addr})", flush=True)
        _refresh_pub_endpoint()
        return 503
    print(f"guest handover completed in {_hand_s:.1f}s ({name} from {addr})", flush=True)
    # handover complete: flip to live, but only if the slot still belongs to
    # this publish (a pusher that died mid-unwind has already moved it to
    # grace via on_publish_done; do not resurrect it)
    refused_for_owner = False
    with _guest_lock:
        # owner-latch re-check INSIDE the critical section: an owner who
        # latched after the admission check up top found this guest still
        # 'free', so its guest_kill armed nothing - flipping live here
        # would put the guest beside the owner with no one left to end it.
        # (_guest_lock -> _owner_lock nesting exists only here; nothing may
        # ever call out of a _guest_lock or _owner_lock section into
        # anything that takes the other - _refresh_pub_endpoint takes
        # _guest_lock, which is why the refresh below sits AFTER the block.)
        with _owner_lock:
            owner_live = _owner["live"]
        if owner_live:
            # end the slot only if it is still THIS publish's handover; a
            # slot that already moved on (grace via a mid-unwind death, or
            # freed by the owner's own preempt) must not be ended twice -
            # that logged a spurious all-None CSV row and re-stamped
            # last_end under someone else's session
            if _guest["state"] == "handover" and _guest["name"] == name:
                _guest_end_locked(OWNER_END_REASON)
            refused_for_owner = True
        elif _guest["state"] == "handover" and _guest["name"] == name:
            _guest.update(state="live", last_seen=time.time())
            _guest_save()
        else:
            print(f"guest vanished during handover: {name}", flush=True)
            return 201          # its session is already closing; nothing to hold
    if refused_for_owner:
        print(f"guest refused at flip: owner went live during handover "
              f"({name} from {addr})", flush=True)
        _refresh_pub_endpoint()
        return 403
    print(f"guest publishing: {name} from {addr}"
          + (" (reconnect)" if resumed_session else ""), flush=True)
    # LIVE ALERT, once per session (mirrors owner_notify's "once per fresh
    # latch, not reconnects" rule): a reconnect within grace is the same
    # session continuing, and alerting on every one would turn a flaky
    # uplink into a buzz storm. Deliberately no security framing here, unlike
    # the owner RTMP-key alert next to this function - the guest app holds no
    # comparable secret to warn about, this is purely "someone is live".
    if not resumed_session:
        telegram(f"guest stream '{name}' is live: from {addr}")
    _guest_stall_arm()
    _refresh_pub_endpoint()
    return 201


def guest_done(name):
    """on_publish_done: enter the reconnect grace, or end at once if this
    session was being terminated (cap hit or operator kill)."""
    if not GUEST_ENABLED:
        return 200
    name = _guest_sanitize(name)
    ended = None
    with _guest_lock:
        if _guest["state"] not in ("live", "handover") \
                or (_guest["name"] and name != _guest["name"]):
            return 200                      # stale/zombie notification
        if _guest["terminating"]:
            ended = _guest["terminating"]
            _guest_end_locked(ended)
        else:
            _guest.update(state="grace", grace_started=time.time())
            _guest_save()
            _grace_timer_arm(GUEST_GRACE_S)
            print(f"guest disconnected: {name}; grace {GUEST_GRACE_S}s", flush=True)
    if ended:
        _resume_after_guest()
    else:
        _refresh_pub_endpoint()
    return 200


def gw_claim(role, name, addr, tracks):
    """/gw/session/claim - the DIRECT path's on_publish. Called by the gateway
    after its probe succeeds and BEFORE it dials earshot, so a refusal here
    means no bytes ever reach the listener.

    Deliberately delegates to guest_publish rather than reimplementing
    admission: ban list, owner lockout, single slot, cooldown, grace-address
    ownership, the loop handover AND its stat-based unwind all come along for
    free, and can never drift from the RTMP path's behaviour because it IS the
    RTMP path's code. (The design doc proposed replacing that unwind with a
    docker-level 'loop container stopped' check; its own review then caught
    that as BLOCKER 3, since the stat is blind to the direct guest but not to
    the LOOP, which is the thing being waited on. Reusing guest_publish means
    the correct check was never at risk.)

    Returns (http_status, body). 409 asks the gateway to retry a transient
    condition, not a refusal: for a guest, the slot could not be cleared
    inside the handover budget, or was taken during it; for an owner, a
    preempted direct guest is still draining."""
    if role == "owner":
        # The owner has no arbiter admission to pass - it authenticates by
        # SRT passphrase and preempts whatever is running. owner_notify arms
        # the guest kill, stops the loop and takes the latch.
        latched = owner_notify(name)
        if not latched:
            # Either the name collided with LOOP_NAME (the demo loop's own
            # branch, which latches nothing) or another owner already holds
            # the latch. Answering 200 here would hand the caller a session
            # id for a latch that does not exist: it would write DASH with
            # stream_state reporting nothing published, so the public start
            # button could put the demo loop beside it, permanently, with
            # nothing to end either. Refuse instead.
            print(f"owner claim refused: '{name}' did not take the latch "
                  f"(reserved name, or another owner holds it)", flush=True)
            return 403, {"error": "owner latch not taken"}
        # PREEMPTION IS NOT INSTANT, and the honest answer is to make the
        # caller wait. owner_notify only ARMS the guest's kill; the guest
        # learns of it on its next beat (<=10 s), its child then gets up to
        # 8 s to exit, and only then does its listener free the port. Saying
        # 200 now would send the owner's child at a port the guest still
        # holds - on the same port that is a refused dial, and on the OTHER
        # port (a 1x4 owner over a 4x4 guest) it is two listener ffmpegs on
        # one DASH tree. 409 uses the gateway's existing fixed-interval retry.
        with _guest_lock:
            draining = _guest["direct"] and _guest["state"] in ("live", "handover")
        if draining:
            return 409, {"retry_after_s": 2}
        with _owner_lock:
            sid = _owner["session"] = uuid.uuid4().hex[:16]
        return 200, {"session": sid}
    if not GUEST_ENABLED:
        return 403, {"error": "guest endpoint disabled"}
    code = guest_publish(name, addr)
    if code == 201:
        with _guest_lock:
            # Mark AFTER guest_publish: it is the RTMP path's own function and
            # knows nothing about direct sessions. Re-check the slot is still
            # ours - a preempt could have landed during the handover.
            if _guest["state"] == "live" and _guest["name"] == name:
                sid = uuid.uuid4().hex[:16]
                _guest.update(direct=True, session=sid, tracks=tracks)
                _guest_save()
                print(f"guest claimed the DIRECT path: {name} from {addr} "
                      f"({tracks} track(s))", flush=True)
                return 200, {"session": sid}
        return 409, {"retry_after_s": 2}
    if code == 503:
        return 409, {"retry_after_s": 2}
    return 403, {"error": "refused"}


def gw_beat(role, session):
    """/gw/session/beat - the DIRECT path's on_update, and the kick lever.

    200 continue, 403 the gateway must terminate the session, 410 the session
    is unknown here and the gateway should re-claim from scratch.

    410 rather than 403 for unknown is BLOCKER 4 from the review: a telemetry
    redeploy loses in-memory state, and answering 403 there would execute every
    live direct session within one beat interval, while answering 200 would
    keep zombies alive forever. 410 says 'I do not know you, re-introduce
    yourself', which the gateway answers with an idempotent re-claim - the
    direct analogue of the RTMP path's stat-backed adoption, which cannot work
    here because a direct session appears in no stat page."""
    if role == "owner":
        with _owner_lock:
            if not _owner["live"]:
                return 410, {}
            if session and _owner["session"] and session != _owner["session"]:
                return 410, {}
            # A same-session beat is the owner keepalive: refresh the latch so
            # owner_tick's ingest probe (which would never see this session)
            # cannot expire it, exactly as the /rtmp/owner/notify re-notify does.
            _owner["since"] = time.time()
            _owner_miss[0] = 0
        return 200, {}
    if not GUEST_ENABLED:
        return 403, {"error": "guest endpoint disabled"}
    with _guest_lock:
        if not _guest["direct"] or not session or _guest["session"] != session:
            return 410, {}
        if _guest["state"] not in ("live", "handover"):
            # demoted to grace by beat silence, or ended outright; the gateway
            # re-claims rather than assuming its slot survived
            return 410, {}
        # Same verdict ladder the RTMP on_update ping enforces, in the same
        # order, so both paths kick for identical reasons: operator kill and
        # End+ban, then the session cap, then the temperature/bitrate guard.
        if _guest["kill"]:
            _guest["terminating"] = _guest["terminating"] or "operator kill"
            _guest_save()
            return 403, {"reason": _guest["terminating"]}
        if _guest["start"] and time.time() - _guest["start"] > GUEST_MAX_S:
            _guest["terminating"] = "session cap"
            _guest_save()
            return 403, {"reason": "session cap"}
        hit = _guest_limits_exceeded()
        if hit:
            _guest["terminating"] = hit
            _guest_save()
            return 403, {"reason": hit}
        _guest["last_seen"] = time.time()
        _guest_save()
        remaining = None
        if _guest["start"] and GUEST_MAX_S:
            remaining = max(0, round(GUEST_MAX_S - (time.time() - _guest["start"])))
    return 200, {"remaining_s": remaining}


def gw_done(role, session):
    """/gw/session/done - the DIRECT path's on_publish_done. Opens the same
    address-locked reconnect grace a dropped RTMP guest gets, or ends the
    session outright if it was already terminating.

    The session id is what makes this safe to send more than once, and is why
    the owner path's double-done timing hack is not needed here: a done for a
    session that is no longer current is simply ignored, rather than being
    guessed at from how recently the latch was refreshed."""
    if role == "owner":
        # Deliberately NOT owner_done(): that path guards against a dead
        # predecessor's late callback by IGNORING any done within 5 s of the
        # latch being (re-)armed, because on the RTMP path a done carries only
        # a name and nothing else can tell the two apart. Here the session id
        # already proves which session is ending, so applying the time
        # heuristic on top would silently drop a legitimate short session's
        # done and strand the latch until owner_tick expired it - which is
        # exactly what testing this route caught (a done 3.6 s after the claim
        # was swallowed, and the next beat answered 200 for a session that had
        # ended). Clear it directly instead; the id IS the identity check.
        resume = False
        with _owner_lock:
            if not _owner["live"]:
                return 200, {}
            if session and _owner["session"] and session != _owner["session"]:
                return 200, {}                  # a dead predecessor's done
            name = _owner["name"]
            _owner.update(live=False, name=None, since=None, session=None)
            _owner_miss[0] = 0
            resume = True
        if resume:
            print(f"owner left (direct session done): {name}", flush=True)
            _resume_after_guest()
        return 200, {}
    with _guest_lock:
        if not _guest["direct"] or not session or _guest["session"] != session:
            return 200, {}
        name = _guest["name"]
    guest_done(name)
    # The gateway waits for its child to exit before sending done, so on THIS
    # path the writer is provably gone and the fence would only recycle an
    # idle listener (and could catch a legitimate next session in a race).
    _direct_fence[0] = False
    return 200, {}


def gw_reject(role, reason):
    """/gw/session/reject - a push the gateway refused BEFORE it could claim.

    The claim is what creates a session here, and the gateway probes the audio
    layout before claiming, so the commonest failure of all (stereo, mono, or
    no audio at all) is refused while telemetry still has nothing to attach a
    reason to. done cannot carry it either: there is no session id to match.

    SRT gives the sender nothing to read. A rejection reason exists only in the
    handshake, and the layout is not known until media has been buffered, so an
    established connection can only be dropped, never explained. The pusher sees
    a bare I/O error. This is the out-of-band path that turns that into a
    sentence on the player page they are already watching.

    Only recorded while the slot is FREE: a live session's own ending outranks a
    stranger's refused handshake, and one publisher at a time means the latest
    refusal is unambiguous without identifying anyone. Which is just as well,
    since the department's UDP forward presents every external guest as the same
    source address."""
    if role != "guest" or not GUEST_ENABLED:
        return 200, {}
    reason = (reason or "").strip()[:200]
    if not reason:
        return 200, {}
    with _guest_lock:
        if _guest["state"] != "free":
            return 200, {}
        _guest.update(last_end=time.time(), last_end_reason=reason)
        _guest_save()
    print(f"guest push refused ({reason})", flush=True)
    return 200, {}


def guest_update(name):
    """on_update liveness ping. Non-2xx here makes nginx-rtmp drop the
    publisher: the enforcement point for the cap and the kill button."""
    if not GUEST_ENABLED:
        return 200
    name = _guest_sanitize(name)
    with _guest_lock:
        if _guest["state"] == "live":
            if _guest["name"] and name != _guest["name"]:
                # a publisher we did not admit (state lost + restored session):
                # drop it rather than let it shadow the tracked one
                return 403
            _guest["last_seen"] = time.time()
            if _guest["kill"]:
                # never clobber a reason set by whoever armed the kill (the
                # stall detector rides the same flag with its own reason)
                _guest["terminating"] = _guest["terminating"] or "operator kill"
                _guest_save()
                return 403
            if _guest["start"] and time.time() - _guest["start"] > GUEST_MAX_S:
                _guest["terminating"] = "session cap"
                _guest_save()
                return 403
            # resource guard, evaluated on the same 10 s ping that enforces the
            # cap and the kill (see GUEST_MAX_TEMP_C / GUEST_MAX_MBPS)
            hit = _guest_limits_exceeded()
            if hit:
                _guest["terminating"] = hit
                _guest_save()
                return 403
            _guest_save()
            return 200
        if _guest["direct"]:
            # a DIRECT session is driven by beats, not by this ping; an RTMP
            # on_update arriving for it (same name, different transport) must
            # not adopt it, re-clock it, or resurrect it from grace
            return 200
        if _guest["state"] == "free":
            # an update with no session: either telemetry lost its state (wiped
            # volume) with a genuine publisher still up, or a stale/forged ping.
            # Only adopt when ingest actually shows a guest publisher, so a
            # delayed ping cannot conjure a phantom session that blocks the slot.
            if not _ingest_guest_publishing():
                return 200
            _guest.update(state="live", name=name, start=time.time(),
                          last_seen=time.time())
            _guest_save()
            print(f"guest session adopted (no state): {name}", flush=True)
            return 200
        # grace, but a publisher is pinging: the done that opened this grace
        # raced a still-live session. Re-adopt it (same clock) so the grace
        # timer cannot expire under an active publisher and restart the loop
        # beside it. Verified against ingest so a delayed ping cannot revive
        # a session whose publisher is truly gone.
        if _guest["state"] == "grace":
            if not _ingest_guest_publishing():
                return 200
            if _guest_timer[0]:
                _guest_timer[0].cancel(); _guest_timer[0] = None
            _guest.update(state="live", name=name, last_seen=time.time(),
                          grace_started=None,
                          start=_guest["start"] or time.time())
            _guest_save()
            print(f"guest re-adopted from grace on update ping: {name}", flush=True)
    return 200


def _guest_limits_exceeded():
    """Resource guard for a live guest, called from the update ping. Returns a
    termination reason (shown to the pusher on the player page) or None.

    Strikes rather than a single sample: temperature swings a few degrees
    between reads and a keyframe can spike the manifest bandwidth, so a limit
    must hold for GUEST_LIMIT_STRIKES consecutive pings (~30 s at the default
    3) before it ends a session. A single good sample resets that limit's
    counter, so a session that backs off is forgiven rather than accumulating
    strikes forever."""
    if not (GUEST_MAX_TEMP_C or GUEST_MAX_MBPS):
        return None
    reason = None

    if GUEST_MAX_TEMP_C:
        t = temp_c()                     # cheap sysfs read, no docker call
        if t is not None and t >= GUEST_MAX_TEMP_C:
            _guest_strikes["temp"] += 1
            print(f"guest resource guard: {t}C >= {GUEST_MAX_TEMP_C}C "
                  f"({_guest_strikes['temp']}/{GUEST_LIMIT_STRIKES})", flush=True)
            if _guest_strikes["temp"] >= GUEST_LIMIT_STRIKES:
                reason = f"host temperature {t}C (limit {GUEST_MAX_TEMP_C}C)"
        else:
            _guest_strikes["temp"] = 0

    if GUEST_MAX_MBPS and not reason:
        # stream_format, not the stream_fmt this line shipped with: the
        # misname raised NameError on every enforcement attempt, the
        # exception aborted the update-ping response, and ingest's 502->204
        # fail-open masking swallowed it - the bitrate guard was inert from
        # the day it landed until 2026-08-09, found by a code audit, not by
        # an incident.
        f = stream_format()              # reads the MPD, no docker call
        bps = (f.get("video_bitrate") or 0) + (f.get("audio_bitrate") or 0)
        m = bps / 1_000_000
        if m > GUEST_MAX_MBPS:
            _guest_strikes["rate"] += 1
            print(f"guest resource guard: {m:.1f} Mbps > {GUEST_MAX_MBPS} Mbps "
                  f"({_guest_strikes['rate']}/{GUEST_LIMIT_STRIKES})", flush=True)
            if _guest_strikes["rate"] >= GUEST_LIMIT_STRIKES:
                reason = (f"stream bitrate {m:.1f} Mbps exceeds this server's "
                          f"limit of {GUEST_MAX_MBPS:g} Mbps")
        else:
            _guest_strikes["rate"] = 0

    return reason


def guest_kill():
    """Dashboard button. A live publisher is dropped at its next update ping
    (<= 10 s); a grace slot is reclaimed immediately."""
    if not GUEST_ENABLED:
        return {"ok": False, "state": "disabled"}
    ended = None
    with _guest_lock:
        if _guest["state"] in ("live", "handover"):
            _guest["kill"] = True
            _guest_save()
            return {"ok": True, "state": "ending", "within_s": 10}
        if _guest["state"] == "grace":
            ended = "operator kill"
            _guest_end_locked(ended)
    if ended:
        _resume_after_guest()
        return {"ok": True, "state": "ended"}
    return {"ok": True, "state": "free"}


def _ingest_owner_publishing(stream_name=None):
    """Does ingest's stat page show a publisher on the OWNER application -
    optionally a publisher under one specific stream name? This is where the
    owner's publisher actually lives, and unlike earshot's stat it survives
    an earshot-only restart (nginx-rtmp resolves its push target once, so
    earshot can look publisher-less forever while the owner is still very
    much connected at ingest). The name-specific form exists for owner_tick:
    an any-publisher check would let the demo loop's own publisher pin a
    stale owner latch forever."""
    x = sh(f"curl -s --max-time 4 {INGEST_STAT}")
    seg = x.split("<name>owner</name>", 1)
    if len(seg) != 2:
        return False
    app = seg[1].split("</application>", 1)[0]
    if stream_name is None:
        return "<publishing/>" in app
    s2 = app.split(f"<name>{stream_name}</name>", 1)
    return len(s2) == 2 and "<publishing/>" in s2[1].split("</stream>", 1)[0]


def _ingest_owner_name():
    """The name of whichever non-loop publisher is currently live on
    ingest's /owner application, or None. _owner["live"] only ever gets SET
    by the one-shot on_publish notify, so a telemetry restart mid-owner-
    session loses it permanently even though the publish itself never
    stopped - measured 2026-08-07: the dashboard read "free" for the rest of
    a session that was live throughout, and worse, guest_publish() only
    consults this same in-memory flag, so a guest could have been admitted
    during that window instead of correctly rejected. owner_tick's re-latch
    check below uses this to notice and repair that, the same way it already
    notices and repairs a latch that outlived its publisher."""
    x = sh(f"curl -s --max-time 4 {INGEST_STAT}")
    seg = x.split("<name>owner</name>", 1)
    if len(seg) != 2:
        return None
    app = seg[1].split("</application>", 1)[0]
    for block in app.split("<stream>")[1:]:
        if "<publishing/>" not in block:
            continue
        nseg = block.split("<name>", 1)
        if len(nseg) != 2:
            continue
        name = nseg[1].split("</name>", 1)[0]
        if name and name != LOOP_NAME:
            return name
    return None


# Compose's default bridge ranges for this stack. Only used to decide whether
# an RTMP owner publish stayed inside the box; a deployment on custom networks
# would fall through and alert, which is the safe direction for a reminder to
# rotate a credential - a spurious nudge costs a moment, a missed one leaves a
# cleartext key spent and unrotated.
_DOCKER_NETS = tuple(ipaddress.ip_network(n) for n in ("172.17.0.0/16",
                                                      "172.18.0.0/16"))


def _rtmp_key_left_the_box(addr):
    """Did this RTMP publisher's address sit outside the docker network - i.e.
    did RTMP_OWNER_KEY cross a wire somebody else could listen on?

    Deliberately NOT the `is_private` test used for viewer counting. The campus
    LAN is RFC1918 and is emphatically not a network this operator controls, so
    a publish from there HAS spent the key in front of strangers and must
    nudge. Only loopback and the compose bridges are treated as inside."""
    try:
        a = ipaddress.ip_address(addr)
    except ValueError:
        return True                      # unparseable: nudge rather than skip
    if a.is_loopback:
        return False
    return not any(a in net for net in _DOCKER_NETS)


def owner_notify(name_arg, addr=None):
    """/rtmp/owner/notify: a publish just passed the key check at rtmp-ingest.

    addr is the publisher's address, forwarded by nginx only on the RTMP path
    (the SRT direct path reaches this through gw_claim and passes none). It
    exists for ONE reason: RTMP carries the owner key in cleartext, so a
    publish that crossed a network has spent the key in front of every hop on
    the way, and the operator should rotate it afterwards. SRT never does -
    its passphrase is libsrt's AES key and stays on the machine - so an SRT
    session must not raise this. Neither must the SRT gateway's own republish
    under SRT_DIRECT=0, which does use RTMP with the token but only over the
    docker network, hence the internal-address check rather than a flag.
    Three cases:
      - no name forwarded (an older nginx template): the legacy behavior,
        an unconditional preempt-at-takeover
      - name == LOOP_NAME: the demo loop. Keep the legacy guest_kill here
        too - it is load-bearing for the one path where the loop starts
        OUTSIDE telemetry's control beside a live guest (`docker compose up
        -d` recreating loop-source), where the old code deterministically
        let the loop win within ~10 s
      - anything else: an EXTERNAL owner. Kill any live guest (with its own
        no-cooldown reason), stop the loop, and latch owner-live so guests
        stay out until the owner leaves."""
    if name_arg is None or name_arg == LOOP_NAME:
        guest_kill()
        return False                     # the demo loop is not an owner latch
    with _owner_lock:
        # first owner wins: a SECOND token-authed publisher under a different
        # name while an owner is latched must not re-point the latch - its
        # later publish_done would then pass owner_done's identity check and
        # clear the latch under the still-live first owner (round-2 finding).
        # Both publishers are the operator's own devices, so just log it.
        # A SAME-name notify is the owner's reconnect: re-arm the latch.
        if _owner["live"] and _owner["name"] != name_arg:
            print(f"second owner publisher ({name_arg}) while "
                  f"{_owner['name']} is latched; latch unchanged", flush=True)
            return False
        # A same-name notify while already latched is the direct path's 30 s
        # keepalive (or an RTMP reconnect): refresh the latch and stop. The
        # takeover side effects below (guest kill, log line, loop-stop
        # thread) must run once per session, not once per keepalive - under
        # the 2026-08-09 keepalive flood each notify forked a docker-stop
        # thread and the log line alone was most of the output. Guests are
        # already locked out and the loop already stopped by the first one.
        rearm = _owner["live"] and _owner["name"] == name_arg
        _owner.update(live=True, name=name_arg, since=time.time())
        _owner_miss[0] = 0
    if rearm:
        return True
    # targeted kill rather than guest_kill(): the preempted guest is
    # innocent, so it gets a reason that (a) shows on the player and (b) is
    # not in _guest_end_locked's cooldown list - no 300 s lockout for the
    # next guest once the owner leaves
    ended = None
    with _guest_lock:
        if _guest["state"] in ("live", "handover"):
            _guest["kill"] = True
            _guest["terminating"] = _guest["terminating"] or OWNER_END_REASON
            _guest_save()
        elif _guest["state"] == "grace":
            ended = OWNER_END_REASON
            _guest_end_locked(ended)
    if ended:
        _refresh_pub_endpoint()     # no resume: the owner holds the slot now
    print(f"owner publishing: {name_arg}; guests locked out", flush=True)
    # the loop stop probes docker and serializes on _start_lock (possibly
    # behind a source_start's `docker start`, up to ~30 s), far over this
    # callback's 3 s nginx budget - and the owner's publish is deliberately
    # not gated on it (fail-open), so do it off-thread. Until the stop lands
    # there is a bounded two-writer window, same as the pre-latch behavior.
    def _handover():
        with _start_lock:
            source_stop("owner handover", kill_after_s=3)
    threading.Thread(target=_handover, daemon=True).start()
    # KEY-SPENT ALERT, once per session (this tail runs only on a fresh latch;
    # reconnects and the direct path's keepalive return above). RTMP has no
    # TLS, so this publish put RTMP_OWNER_KEY on the wire in cleartext for
    # every hop between the publisher and this box to read. Rotating
    # afterwards is the whole mitigation, and the thing most likely to defeat
    # it is simply forgetting - so the box says so at the moment it happens
    # rather than relying on the operator to remember days later.
    if addr and _rtmp_key_left_the_box(addr):
        telegram(f"🔑 owner published over RTMP from {addr} as '{name_arg}'.\n"
                 f"RTMP sends RTMP_OWNER_KEY in CLEARTEXT - it has now been "
                 f"exposed to every hop on that path. Rotate it when this "
                 f"session ends:\n"
                 f"  ssh box; cd ~/ambi-box\n"
                 f"  sed -i \"s|^RTMP_OWNER_KEY=.*|RTMP_OWNER_KEY=$(openssl rand -hex 24)|\" .env\n"
                 f"  docker compose up -d\n"
                 f"(SRT does not need this - its passphrase never leaves your machine.)")
    return True


def owner_done(name_arg):
    """/rtmp/owner/done: a /owner publisher left. The demo loop's own
    unpublish needs nothing here (telemetry drives the loop itself); the
    LATCHED owner leaving clears the latch and hands the slot back through
    the same resume rule a guest's end uses (loop only if somebody watches).
    Two guards against clearing the wrong session:
      - identity: only the latched owner's own name may clear (a second
        token-authed publisher under another name coming and going must not)
      - freshness: on an encoder reconnect nginx can deliver the OLD
        connection's done after the NEW connection's notify re-latched;
        a done arriving within seconds of the latch belongs to that dead
        predecessor, not to the owner now live. A genuinely aborted <5 s
        session leaves the latch to the owner_tick backstop instead."""
    if name_arg is None or name_arg == LOOP_NAME:
        return
    resume = False
    with _owner_lock:
        if not _owner["live"] or name_arg != _owner["name"]:
            return
        if time.time() - (_owner["since"] or 0) < 5:
            print(f"owner done ignored (stale, latch re-armed): {name_arg}",
                  flush=True)
            return
        _owner.update(live=False, name=None, since=None)
        _owner_miss[0] = 0
        resume = True
    if resume:
        print(f"owner left: {name_arg}", flush=True)
        _resume_after_guest()


def _owner_relatch_check():
    """The other direction of owner_tick's backstop: re-derive the latch
    from ingest's own publisher list when it is currently UNSET, so a
    telemetry restart mid-owner-session cannot leave it permanently wrong
    (see _ingest_owner_name's docstring - this was observed live on
    2026-08-07, not theoretical).

    Deliberately does NOT repeat owner_notify's takeover side effects
    (guest-kill, loop-stop): if the session predates this telemetry
    process, those already ran once against the real event, and redoing
    them here is not needed in the common case, only the latch state is
    actually missing. Residual risk, accepted rather than chased: a guest
    admitted during the exact gap between the restart and this check
    firing (bounded by one INTERVAL) is not retroactively evicted. Narrow,
    and this whole scenario has only been observed following telemetry's
    own redeploys, never in normal operation."""
    name = _ingest_owner_name()
    if not name:
        return
    with _owner_lock:
        if _owner["live"]:
            return              # set by a real notify while we were probing
        _owner.update(live=True, name=name, since=time.time())
        _owner_miss[0] = 0
    print(f"owner latch re-derived from ingest (name={name}); a telemetry "
          f"restart most likely dropped the original notify", flush=True)


def owner_tick():
    """Per-cycle backstop for the latch: if the owner's publish_done was
    lost (an rtmp-ingest restart is the realistic path), the latch would
    lock guests out and hold the loop down forever. Clear it once a settled
    latch (>60 s old) has shown no owner-app publisher UNDER THE LATCHED NAME
    at ingest on two consecutive cycles - name-specific, because the demo
    loop publishes to the same application and an any-publisher probe would
    let a compose-recreated loop pin a stale latch forever (round-2 finding).
    The clear is identity-checked on the latch's own `since`: a fresh notify
    re-arming the latch mid-probe changes it, and the stale clear must then
    abort rather than wipe the new session. The miss counter is only touched
    under the same identity check, so a probe overlapping a session change
    can never lend its miss to the wrong session.

    When the latch is unset this defers to _owner_relatch_check instead -
    the mirror-image backstop, for when telemetry itself is what lost track,
    not the publisher."""
    with _owner_lock:
        live, since, oname = _owner["live"], _owner["since"], _owner["name"]
    if not live:
        _owner_relatch_check()
        return
    if time.time() - (since or 0) < 60:
        return
    publishing = _ingest_owner_publishing(oname)
    with _owner_lock:
        if not _owner["live"] or _owner["since"] != since:
            return                  # re-latched mid-probe; not ours to touch
        if publishing:
            _owner_miss[0] = 0
            return
        _owner_miss[0] += 1
        if _owner_miss[0] < 2:
            return
        _owner.update(live=False, name=None, since=None)
        _owner_miss[0] = 0
    print("owner latch cleared by backstop (no owner-app publisher at ingest)",
          flush=True)
    _resume_after_guest()


# --- ban store ---------------------------------------------------------
# guest_bans.csv rows: banned_at,ip,cc,expires_at,reason,state
# Three end states: active (enforced, IP present), unbanned (lifted early,
# IP kept until redaction, NOT enforced), expired (retention elapsed, IP
# redacted in the same operation that writes the label, NOT enforced).
# Enforcement deliberately does NOT trust the label alone: it blocks only
# rows that are active AND still carry an IP AND whose expires_at is in the
# future, so a stale label after a missed job cycle can never cause a
# wrongful block.

_bans_lock = threading.Lock()

def _bans_read():
    rows = []
    try:
        for line in BANSCSV.read_text().splitlines():
            p = line.split(",")
            if len(p) >= 6:
                rows.append({"banned_at": p[0], "ip": p[1], "cc": p[2],
                             "expires_at": p[3], "reason": p[4], "state": p[5]})
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return rows

def _bans_write(rows):
    tmp = BANSCSV.with_suffix(".csv.tmp")
    tmp.write_text("".join(
        f'{r["banned_at"]},{r["ip"]},{r["cc"]},{r["expires_at"]},{r["reason"]},{r["state"]}\n'
        for r in rows))
    tmp.replace(BANSCSV)

def _ban_blocks(ip):
    """The enforcement rule, all three conditions explicit."""
    if not ip:
        return False
    now = time.time()
    for r in _bans_read():
        if r["state"] != "active":
            continue
        if r["ip"] in ("-", "") or r["ip"] != ip:
            continue
        try:
            if datetime.fromisoformat(r["expires_at"]).timestamp() > now:
                return True
        except Exception:
            continue
    return False

def _bans_active_ips():
    """Currently enforceable ban addresses - the same three explicit
    conditions as _ban_blocks, list form, for the gateway's snapshot."""
    now = time.time()
    ips = []
    for r in _bans_read():
        if r["state"] != "active" or r["ip"] in ("-", ""):
            continue
        try:
            if datetime.fromisoformat(r["expires_at"]).timestamp() > now:
                ips.append(r["ip"])
        except Exception:
            continue
    return ips


def guest_precheck_snapshot():
    """Read-only admission state for srt-gateway's handshake pre-filter,
    polled every couple of seconds. Same compose-internal trust boundary as
    every /rtmp/guest/ route (the ban IPs it carries stay inside it). Only a
    pre-filter: the authoritative fail-closed gate remains guest_publish when
    the gateway's relay actually connects."""
    if not GUEST_ENABLED:
        return {"enabled": False, "available": False,
                "grace_addr": None, "bans": []}
    with _guest_lock:
        st = _guest["state"]
        cd = _guest.get("cooldown_until")
        available = st in ("free", "grace") and not (cd and time.time() < cd)
        grace_addr = _guest.get("addr") if st == "grace" else None
    # owner-live latch: reflected here too so srt-gateway rejects guests at
    # the SRT handshake instead of spawning a relay that guest_publish will
    # only refuse a second later
    with _owner_lock:
        if _owner["live"]:
            available = False
    return {"enabled": True, "available": available,
            "grace_addr": grace_addr, "bans": _bans_active_ips()}


def _bans_expire():
    """Retention-window sweep: past-retention rows lose their IP, and rows
    still labelled active get state=expired IN THE SAME WRITE, so the CSV
    stays self-describing (unbanned rows keep their outcome label)."""
    with _bans_lock:
        rows = _bans_read()
        if not rows:
            return
        cutoff = time.time() - GUEST_RETENTION_S
        changed = 0
        for r in rows:
            try:
                old = datetime.fromisoformat(r["banned_at"]).timestamp() < cutoff
            except Exception:
                old = False
            if not old:
                continue
            if r["ip"] not in ("-", "") and not r["ip"].endswith((".x", "::x")):
                r["ip"] = anon_ip(r["ip"]); changed += 1
            if r["state"] == "active":
                r["state"] = "expired"; changed += 1
        if changed:
            _bans_write(rows)
            print(f"guest_bans.csv: {changed} field(s) expired/redacted", flush=True)

def guest_ban():
    """Dashboard 'End + ban': ban the current session's address, then ride
    the normal kill path. Works in grace too (the publisher is gone but the
    session, and its address, still exist)."""
    if not GUEST_ENABLED:
        return {"ok": False, "error": "guest endpoint disabled"}
    with _guest_lock:
        if _guest["state"] not in ("live", "handover", "grace"):
            return {"ok": False, "error": "no active session"}
        addr, name = _guest.get("addr"), _guest.get("name")
    if not addr:
        return {"ok": False, "error": "session has no recorded address"}
    now = datetime.now().astimezone()
    exp = now + timedelta(days=GUEST_BAN_DAYS)
    with _bans_lock:
        rows = _bans_read()
        rows.append({"banned_at": now.isoformat(timespec="seconds"),
                     "ip": addr, "cc": geo_cc(addr),
                     "expires_at": exp.isoformat(timespec="seconds"),
                     "reason": "operator ban", "state": "active"})
        _bans_write(rows)
    print(f"guest banned: {addr} until {exp.isoformat(timespec='seconds')} "
          f"(session '{name}')", flush=True)
    out = guest_kill()
    out["banned"] = addr
    return out

def guest_unban(ip):
    if not GUEST_ENABLED:
        return {"ok": False, "error": "guest endpoint disabled"}
    if not ip:
        return {"ok": False, "error": "ip required"}
    with _bans_lock:
        rows = _bans_read()
        hit = 0
        for r in rows:
            if r["state"] == "active" and r["ip"] == ip:
                r["state"] = "unbanned"; hit += 1
        if hit:
            _bans_write(rows)
    print(f"guest unban: {ip} ({hit} row(s))", flush=True)
    return {"ok": bool(hit), "unbanned": hit}

def _bans_lists():
    """Dashboard split: active = the enforcement view (same triple rule, so
    a stale label can never grow an Unban button on a redacted row);
    history = everything else, no IPs, outcome computed by time when the
    label lags the clock."""
    now = time.time()
    active, history = [], []
    for r in _bans_read():
        live = False
        if r["state"] == "active" and r["ip"] not in ("-", ""):
            try:
                live = datetime.fromisoformat(r["expires_at"]).timestamp() > now
            except Exception:
                live = False
        if live:
            active.append({"ip": r["ip"], "cc": r["cc"], "banned_at": r["banned_at"],
                           "expires_at": r["expires_at"], "reason": r["reason"]})
        else:
            outcome = r["state"]
            if r["state"] == "active":
                outcome = "expired"          # label lagging a missed sweep
            history.append({"cc": r["cc"], "banned_at": r["banned_at"],
                            "expires_at": r["expires_at"], "reason": r["reason"],
                            "outcome": outcome})
    return {"active": active, "history": history}


def _guest_stall_arm():
    """One-shot check GUEST_STALL_S after a session goes live: if no playable
    segment has appeared, the transcoder is stalled (in practice: the pusher
    sent a layout the chain cannot carry - stereo or mono, where an ambisonic
    4 or 16 channels is required) and the session would
    otherwise squat the slot for the full cap with nothing playing. End it and
    surface the reason (drill finding: the commonest real failure was also the
    most confusing, because OBS reports a successful connection)."""
    if _stall_timer[0]:
        _stall_timer[0].cancel()
    def check():
        with _guest_lock:
            if _guest["state"] != "live":
                return
            age = segment_age()
            if age is not None and age < SEG_STALE_S:
                return              # segments flowing; healthy session
            _guest["terminating"] = "no playable output (ambisonic audio required: 4 or 16 channels)"
            _guest["kill"] = True   # dropped at the next update ping
            _guest_save()
        print("guest transcode stalled; ending the session "
              "(wrong audio layout is the usual cause)", flush=True)
        telegram(f"guest stream '{_guest.get('name')}' ended automatically: "
                 "no playable output (wrong audio layout?)")
    t = threading.Timer(GUEST_STALL_S, check)
    t.daemon = True
    t.start()
    _stall_timer[0] = t


def guest_report(reporter_ip, reporter_cc):
    """The player's report button. Rate limits: nginx already brakes per
    viewer IP; here, at most REPORT_IP_MAX accepted per reporter IP per
    window, and one Telegram alert per session per REPORT_COOLDOWN_S with an
    escalating count. Reporter IPs land in guest_reports.csv under the same
    30-day redaction as everything else."""
    if not GUEST_ENABLED:
        return 404, {"ok": False}
    now = time.time()
    with _guest_lock:
        if _guest["state"] not in ("live", "handover", "grace"):
            return 409, {"ok": False, "reason": "no active session"}
        hits = [t for t in _reporters.get(reporter_ip, []) if now - t < REPORT_IP_WINDOW_S]
        if len(hits) >= REPORT_IP_MAX:
            return 429, {"ok": False, "reason": "already reported"}
        hits.append(now)
        _reporters[reporter_ip] = hits
        _guest["reports"] += 1
        n = _guest["reports"]
        alert = (_guest["last_report_alert"] is None
                 or now - _guest["last_report_alert"] > REPORT_COOLDOWN_S)
        if alert:
            _guest["last_report_alert"] = now
        name, addr, start = _guest.get("name"), _guest.get("addr"), _guest.get("start")
        _guest_save()
    pub_cc = geo_cc(addr or "")
    rep_cc = reporter_cc or geo_cc(reporter_ip or "")
    try:
        with open(REPORTCSV, "a") as f:
            f.write(f"{now_iso()},{name},{addr},{pub_cc},{reporter_ip},{rep_cc},{1 if alert else 0}\n")
    except Exception:
        pass
    if alert:
        elapsed = dur_short(now - start) if start else "?"
        # Same shape as the footer's Report Date, and local time like it: an ISO
        # string with a +00:00 offset next to a footer reading local time made
        # one message carry two clocks, two hours apart, with only the offset to
        # tell you. TZ is set on the container so both are the deployment's own
        # time (see docker-compose.yml).
        started = datetime.fromtimestamp(start).astimezone().strftime("%Y-%m-%d %H:%M:%S") if start else "?"
        # no dashboard mention in the body: the telegram() tail appends the
        # actual link, and its 8090-dedup guard must not be tripped here
        telegram(f"guest stream REPORTED: '{name}' from {addr} ({pub_cc})\n"
                 f"started {started}, running {elapsed}\n"
                 f"reporter: {reporter_ip} ({rep_cc})\n"
                 f"reports so far: {n}")
    return 200, {"ok": True, "reported": True}


# Service restarts from the dashboard. Same trust boundary as /api/stop.
# earshot is only offered TOGETHER with ingest: nginx-rtmp resolves the push
# hostname once at startup, so a lone earshot restart silently breaks the
# relay (the standing rule, now encoded rather than remembered).
# srt-gateway restarts alone safely: it holds no resolve-once state (its
# per-session ffmpeg dials rtmp-ingest fresh each time), so the only cost is
# dropping an SRT session in progress. srt-gateway-owner is the same
# gateway.py, same reasoning, just SRT_MODE=owner instead of guest - not in
# the base compose file, only present when the private override runs it.
RESTARTABLE = {"rtmp-ingest": ["rtmp-ingest"],
               "hoast-player": ["hoast-player"],
               "telemetry": ["telemetry"],
               "srt-gateway": ["srt-gateway"],
               "srt-gateway-owner": ["srt-gateway-owner"],
               "earshot-ingest": ["earshot", "rtmp-ingest"]}


def _service_container(svc):
    out = sh(f'docker ps -a --filter "label=com.docker.compose.project={PROJECT}" '
             f'--filter "label=com.docker.compose.service={svc}" '
             f'--filter "label=com.docker.compose.oneoff=False" --format "{{{{.Names}}}}"')
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return lines[0] if lines else ""


def restart_services(key):
    plan = RESTARTABLE.get(key)
    if not plan:
        return {"ok": False, "error": "unknown service"}
    names = [(_service_container(svc), svc) for svc in plan]
    missing = [svc for (n, svc) in names if not n]
    if missing:
        return {"ok": False, "error": "container not found: " + ", ".join(missing)}
    def do():
        for n, svc in names:
            sh(f"docker restart {n}", t=90)
            print(f"dashboard restart: {svc} ({n})", flush=True)
    if key == "telemetry":
        # respond first, restart ourselves after the reply has left
        threading.Timer(0.5, do).start()
        return {"ok": True, "state": "restarting", "note": "telemetry back in ~10 s"}
    do()
    return {"ok": True, "state": "restarted", "services": plan}


def _guest_boot():
    """Reload persisted state and reconcile it against reality: a session that
    died while telemetry was down must not hold the slot forever."""
    if not GUEST_ENABLED:
        return
    try:
        st = json.loads(GSTATE.read_text())
        with _guest_lock:
            _guest.update({k: st.get(k, _guest[k]) for k in _guest})
    except Exception:
        return
    with _guest_lock:
        if _guest["state"] in ("live", "handover"):
            if _guest["direct"]:
                # A DIRECT session appears in no stat page, so this probe would
                # demote every one of them on every restart. Its own liveness
                # check is the beat: guest_tick's 60 s silence rule ends it if
                # the gateway is really gone, and if the gateway is still there
                # the next beat re-confirms the session (or, when the state was
                # lost entirely, is answered 410 and the gateway re-claims).
                # Stamp last_seen so the silence rule measures from the restart
                # rather than from a possibly-ancient persisted value.
                _guest["last_seen"] = time.time()
                _guest_save()
                print("direct guest session restored after restart; "
                      "awaiting beat", flush=True)
            elif not _ingest_guest_publishing():
                _guest.update(state="grace", grace_started=time.time())
                _guest_save()
                _grace_timer_arm(GUEST_GRACE_S)
                print("guest session not found on ingest after restart; grace", flush=True)
            elif _guest["state"] == "handover":
                # publisher exists and we died mid-flip: it is effectively live
                _guest.update(state="live", last_seen=time.time())
                _guest_save()
        elif _guest["state"] == "grace" and _guest["grace_started"]:
            left = GUEST_GRACE_S - (time.time() - _guest["grace_started"])
            _grace_timer_arm(max(0.5, left))


def stream_format():
    """Everything the panels report about the stream, read from the live MPD.

    The manifest is ground truth. Deriving these from FFMPEG_FLAGS instead
    reports what the encoder was told to do, which is wrong whenever the video is
    stream-copied (no scale= or -b:v appears in the flags at all) and stale
    whenever the config changes without recreating this container. Reading the
    manifest also means an arbitrary stream pointed at this server is described
    correctly, including a different ambisonic order.

    Note the ACN/SN3D ordering is a stack convention rather than something the
    stream signals: the audio is Opus mapping family 255 (discrete channels), so
    only the channel COUNT is carried. Order is inferred from it.
    """
    out = {"resolution": None, "video_bitrate": None, "audio_bitrate": None,
           "video_codec": None, "audio_codec": None, "audio_channels": None,
           "sample_rate": None, "ambisonic_order": None, "spatial_audio": None}
    try:
        mpds = sorted(DASH.glob("*.mpd"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mpds:
            return out
        root = ET.parse(mpds[0]).getroot()
        vid, aud = [], []
        for aset in root.iter():
            if not aset.tag.endswith("AdaptationSet"):
                continue
            aw, ah = aset.get("width"), aset.get("height")   # may sit on either element
            for rep in aset:
                if not rep.tag.endswith("Representation"):
                    continue
                bw = rep.get("bandwidth")
                if not bw:
                    continue
                cod = rep.get("codecs") or aset.get("codecs")
                w, h = rep.get("width") or aw, rep.get("height") or ah
                if w and h:
                    vid.append((int(bw), w, h, cod))
                else:
                    ch = None
                    for kid in rep:
                        if kid.tag.endswith("AudioChannelConfiguration") and kid.get("value"):
                            ch = int(kid.get("value"))
                    aud.append((int(bw), cod, ch, rep.get("audioSamplingRate")))
        if vid:
            vbw, w, h, vcod = max(vid, key=lambda t: t[0])
            out.update(resolution=f"{w}x{h}", video_bitrate=vbw, video_codec=codec_name(vcod))
        if aud:
            abw, acod, ch, sr = max(aud, key=lambda t: t[0])
            out.update(audio_bitrate=abw, audio_channels=ch,
                       audio_codec=(acod.split(".")[0].capitalize() if acod else None),
                       sample_rate=int(sr) if sr else None)
            if ch:
                r = int(round(ch ** 0.5))
                if r * r == ch and r >= 2:       # perfect square => a full ambisonic set
                    n = r - 1
                    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n, "th")
                    out["ambisonic_order"] = n
                    out["spatial_audio"] = f"{n}{suffix}-order Ambisonics"
        return out
    except Exception:
        return out


def mbps(bps):
    """None means unknown and renders as a dash; 0 is a real answer. A live stream
    with nobody watching has an egress of zero, which is worth stating."""
    return f"{bps / 1_000_000:.1f} Mbps" if bps is not None else None


def codec_name(c):
    """avc1.640033 -> H.264, av01.0.08M.08 -> AV1, vp09/vp9 -> VP9."""
    if not c:
        return None
    c = c.lower()
    for pfx, name in (("avc", "H.264"), ("hev", "HEVC"), ("hvc", "HEVC"),
                      ("av01", "AV1"), ("vp9", "VP9"), ("vp09", "VP9"), ("vp8", "VP8")):
        if c.startswith(pfx):
            return name
    return c.split(".")[0]


def telegram(msg):
    if not BOT or not CHAT:
        return
    # Dashboard link tail. Telegram only auto-linkifies hostnames with a
    # real TLD (IPs work, a bare TLD-less hostname never does), so deployments
    # set TEL_DASH_URL to a resolvable full URL (e.g. the Tailscale MagicDNS
    # name); plain text needs no parse_mode and nothing to escape.
    tail = os.environ.get("TEL_DASH_URL", "")
    if not tail:
        _h = os.environ.get("TEL_HOST", "")
        tail = f"http://{_h}:8090/" if _h else ""
    # FOOTER, in the same shape the operator's other senders use, so every
    # machine on the one Telegram chat reads alike:
    #
    #     Link (TS):   100.113.245.44:8090
    #     Report Date: 2026-08-11 12:14:16
    #     Server:      ambisonic-box
    #
    # Labels padded so the values line up at one column, blank line before.
    # A BARE ip:port, no scheme: Telegram linkifies that, and it is far shorter
    # than the MagicDNS name it replaced. Only one of the three call sites here
    # used to name the host at all, so guest auto-end and guest-report alerts
    # arrived with no way to tell which machine sent them. The report date is
    # DETECTION time, which is not Telegram's delivery time when a send is
    # retried or the network stalls.
    if HOST and f"Server:" not in msg:
        when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        foot = []
        if tail:
            foot.append(f"{'Link (TS):':<13}{tail}")
        foot.append(f"{'Report Date:':<13}{when}")
        foot.append(f"{'Server:':<13}{HOST}")
        msg = msg + "\n\n" + "\n".join(foot)
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": msg}).encode()
    # One retry, then a log line. A swallowed failure here used to be
    # invisible, which on the alerting path is the worst possible place for
    # silence: the operator reads "no message" as "all healthy". A print is
    # enough - it lands in `docker logs`, which the dead-man's journal and a
    # human debugging "why no Telegram" both reach. Deliberately no queue or
    # long backoff: alerts are only useful fresh, and the next collect tick
    # re-alerts anyway if the condition persists.
    for attempt in (1, 2):
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{BOT}/sendMessage", data=data, timeout=10)
            return
        except Exception as e:
            if attempt == 2:
                print(f"telegram send FAILED twice ({e.__class__.__name__}: {e}); alert not delivered: {msg[:80]!r}", flush=True)
            else:
                time.sleep(2)

def dur_short(sec):
    """Compact episode length: 45s, 4m, 1h12m."""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


# How to phrase the worst reading of an episode, per condition. Telegram stamps
# every message with its own send time, so a timestamp in the body would only
# duplicate what is printed directly above it. What the stamps cannot show at a
# glance is how long the problem lasted and how bad it got, which is what the
# recovery line carries instead.
WORST_FMT = {
    "encoder_behind": lambda v: f"worst {v:.2f}x",
    "overheat":       lambda v: f"peak {v}°C",
    "disk_full":      lambda v: f"peak {v}%",
    "stream_stalled": lambda v: f"worst {v}s stale",
}


def tunnel_probe():
    """Cloudflared health via its local metrics server: /ready gives the
    connection count, /metrics the edge locations. The tunnel dropping is the
    one failure where the box looks healthy to itself while being unreachable
    to everyone else, hence its own panel and alert."""
    if not TUNNEL_METRICS_URL:
        return None
    # connected: True/False from a successful metrics read; None when the
    # metrics endpoint itself is unreachable. The distinction matters for the
    # alert: an unreachable metrics port must read as UNKNOWN, never as
    # "tunnel down", or a probe misconfiguration would page the operator.
    out = {"connected": None, "conns": 0, "locations": [],
           "checked": now_iso()}
    try:
        r = urllib.request.urlopen(f"{TUNNEL_METRICS_URL}/ready", timeout=4)
        j = json.loads(r.read().decode())
        out["conns"] = int(j.get("readyConnections", 0))
        out["connected"] = out["conns"] > 0
    except urllib.error.HTTPError as e:
        # cloudflared answers 503 on /ready when it has no connections:
        # that IS a definite "down", not an unknown
        if e.code == 503:
            out["connected"] = False
        return out
    except Exception:
        return out
    try:
        m = urllib.request.urlopen(f"{TUNNEL_METRICS_URL}/metrics", timeout=4).read().decode()
        locs = set()
        for line in m.splitlines():
            if line.startswith("cloudflared_tunnel_server_locations{") and line.rstrip().endswith(" 1"):
                i = line.find('edge_location="')
                if i >= 0:
                    locs.add(line[i + 15:line.index('"', i + 15)])
        out["locations"] = sorted(locs)
    except Exception:
        pass
    return out


def vod_origin_probe():
    """HEAD on one known VOD object. Not a usage metric, just reachable or
    not; if it fails, the actionable response is removing vodBase from
    brand.json so the player falls back to box-served VOD."""
    if not VOD_PROBE_URL:
        return None
    out = {"ok": False, "code": None, "checked": now_iso(), "url": VOD_PROBE_URL}
    # Self-announcing DNS pin: if the probe hostname is overridden in this
    # container's /etc/hosts (extra_hosts in an override), say so in the
    # panel on every view. A workaround that only lives in docs rots
    # silently; one the dashboard keeps naming gets removed when its reason
    # (the originating ticket) closes.
    try:
        host = urllib.parse.urlparse(VOD_PROBE_URL).hostname or ""
        pins = []
        for line in open("/etc/hosts"):
            p = line.split()
            if len(p) >= 2 and host and host in p[1:]:
                pins.append(p[0])
        if pins:
            out["pinned_ips"] = pins
            # optional deployment-specific context for the dashboard note
            # (e.g. the ticket that justifies the pin); generic deployments
            # simply have none
            note = os.environ.get("TEL_PIN_NOTE", "")
            if note:
                out["pin_note"] = note
            url = os.environ.get("TEL_PIN_URL", "")
            if url:
                out["pin_url"] = url
    except Exception:
        pass
    try:
        # Cloudflare's bot rules 403 the default Python-urllib agent
        req = urllib.request.Request(VOD_PROBE_URL, method="HEAD",
                                     headers={"User-Agent": "ambi-box-telemetry/1.0"})
        r = urllib.request.urlopen(req, timeout=6)
        out["code"] = r.status
        out["ok"] = 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        out["code"] = e.code
    except Exception:
        pass
    return out


_cfa_cache = [0.0, None]     # (fetched_at, last good result)

def cf_vod_analytics():
    """VOD page loads + country breakdown from Cloudflare Web Analytics
    (rumPageloadEventsAdaptiveGroups, last 24 h, requestPath /vod*). The
    dataset is marked Beta, so every access is defensive: on any shape
    change or API error the panel keeps the last good numbers marked stale
    instead of crashing. Polled at most every 5 min."""
    if not CF_ANALYTICS_TOKEN or not CF_ACCOUNT_ID:
        return None
    now = time.time()
    if now - _cfa_cache[0] < 300:
        return _cfa_cache[1]
    _cfa_cache[0] = now
    since = datetime.fromtimestamp(now - 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = {
        "query": """query($acct: String!, $since: Time!, $until: Time!) {
          viewer { accounts(filter: {accountTag: $acct}) {
            rumPageloadEventsAdaptiveGroups(limit: 100, filter: {AND: [
              {datetime_geq: $since}, {datetime_leq: $until},
              {requestPath_like: \"/vod%\"}]}) {
              count
              sum { visits }
              dimensions { countryName }
            } } } }""",
        "variables": {"acct": CF_ACCOUNT_ID, "since": since, "until": until},
    }
    try:
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/graphql",
            data=json.dumps(q).encode(),
            headers={"Authorization": f"Bearer {CF_ANALYTICS_TOKEN}",
                     "Content-Type": "application/json",
                     "User-Agent": "ambi-box-telemetry/1.0"})
        j = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        groups = j["data"]["viewer"]["accounts"][0]["rumPageloadEventsAdaptiveGroups"]
        views, visits, countries = 0, 0, {}
        for g in groups:
            n = int(g.get("count") or 0)
            views += n
            visits += int((g.get("sum") or {}).get("visits") or 0)
            cn = ((g.get("dimensions") or {}).get("countryName") or "").strip()
            if cn:
                countries[cn] = countries.get(cn, 0) + n
        top = dict(sorted(countries.items(), key=lambda kv: -kv[1])[:6])
        out = {"views": views, "visits": visits, "countries": top,
               "window_h": 24, "checked": now_iso(), "stale": False}
        _cfa_cache[1] = out
        return out
    except Exception as e:
        print(f"cf web analytics poll failed (kept last good): {type(e).__name__}", flush=True)
        prev = _cfa_cache[1]
        if prev:
            prev = {**prev, "stale": True}
            _cfa_cache[1] = prev
        return prev


def backup_check():
    if not BACKUP_MARKER:
        return None
    out = {"max_age_h": BACKUP_MAX_AGE_H, "age_h": None, "stale": True}
    try:
        age = time.time() - os.path.getmtime(BACKUP_MARKER)
        out["age_h"] = round(age / 3600, 1)
        out["stale"] = age > BACKUP_MAX_AGE_H * 3600
    except OSError:
        pass                     # marker missing: stale stays True
    return out


def publisher_label():
    """Who is publishing right now, in words, for alert messages.

    Added 2026-08-11 after the operator received `stream publishing but
    segments 757s stale` and had to ask which sender that was - twice in one
    day, because a test suite was running at the time. An alert that names the
    host but not the publisher makes the reader do the lookup at the one moment
    they are least able to.

    Reads the module state rather than the payload: collect_once fills
    s["endpoint"] AFTER evaluate_alerts, so the payload has no publisher yet at
    the moment an alert is built. Takes the two locks SEPARATELY and never
    nested - guest_publish nests _guest_lock -> _owner_lock, so nesting the
    other way here would be the AB-BA deadlock the round-3 review caught.
    """
    try:
        with _guest_lock:
            if _guest.get("state") == "live":
                name, addr = _guest.get("name"), _guest.get("addr")
                return f"guest '{name}'" + (f" from {addr}" if addr else "")
        with _owner_lock:
            if _owner.get("live"):
                return "the operator's own stream"
        if source_container(running_only=True):
            return "the demo loop"
    except Exception:
        pass          # an alert must never fail to send because of its own label
    return "an unidentified source"      # reads correctly in "... publishing but ..."


def evaluate_alerts(s):
    try:
        state = json.loads(STATE.read_text())
    except Exception:
        state = {}
    counts, fired = state.get("_counts", {}), state.get("_fired", {})
    since, worst = state.get("_since", {}), state.get("_worst", {})
    active, msgs = [], []
    down = [f"{x['name']} ({x.get('health') or x.get('state') or 'down'})"
            for x in s["services"] if not x["healthy"]]
    d, t = s["system"]["disk_used_pct"], s["system"]["temp_c"]
    st = s["stream"]
    # WHO IS RESPONSIBLE FOR THE GAP. segment_age_s is the age of the newest
    # segment on disk, and it keeps growing while the slot is empty and the loop
    # is idle. A publisher that has only just claimed inherits all of it: on
    # 2026-08-23 a guest was alerted as "publishing but segments 33s stale" four
    # seconds after connecting, for 29 s that accrued while the slot was free.
    # A publisher can only be blamed for staleness it had time to cause, so the
    # condition also requires it to have been publishing at least as long as the
    # threshold. A genuine stall still trips, SEG_STALE_S later.
    _pub_since = state.get("_pub_since")
    if not st["publishing"]:
        _pub_since = None
    elif _pub_since is None:
        _pub_since = time.time()
    state["_pub_since"] = _pub_since
    _pub_for = (time.time() - _pub_since) if _pub_since else 0
    stalled = bool(st["publishing"] and st["segment_age_s"] is not None
                   and st["segment_age_s"] > SEG_STALE_S and _pub_for > SEG_STALE_S)
    # Trailing pair is the reading to track across the episode and whether lower
    # or higher is worse, so the recovery line can report how bad it got.
    _pub = publisher_label()
    conds = {
        "services_down":  (bool(down), "service(s) unhealthy: " + ", ".join(down), "all services healthy again", None, None),
        "disk_full":      (d is not None and d >= DISK_FULL_PCT, f"disk {d}% full", "disk usage back to normal", d, "max"),
        "overheat":       (t is not None and t >= TEMP_CRIT_C, f"CPU {t}°C, at/above the alert threshold", "CPU temp back below 100°C", t, "max"),
        "encoder_behind": (s["encoder"]["behind"], f"encoder behind realtime ({s['encoder']['speed_now']}x now, {s['encoder']['speed']}x lifetime avg) on {_pub}", "encoder keeping up again", s["encoder"]["speed_now"], "min"),
        "stream_stalled": (stalled, f"{_pub} publishing but segments {st['segment_age_s']}s stale", "stream flowing again", st["segment_age_s"], "max"),
        "tunnel_down":    (bool(s.get("tunnel")) and s["tunnel"]["connected"] is False, "cloudflared tunnel DISCONNECTED: box healthy but unreachable from outside", "tunnel reconnected", (s.get("tunnel") or {}).get("conns"), "min"),
        "backup_stale":   (bool(s.get("backup")) and s["backup"]["stale"], f"telemetry backup STALE: last successful pull {(s.get('backup') or {}).get('age_h')} h ago (limit {BACKUP_MAX_AGE_H} h)", "backup pulls resumed", (s.get("backup") or {}).get("age_h"), "max"),
    }
    for key, (cond, problem, recovered, metric, worse) in conds.items():
        counts[key] = (counts.get(key, 0) + 1) if cond else 0
        if cond and metric is not None:
            prev = worst.get(key)
            if prev is None or (metric < prev if worse == "min" else metric > prev):
                worst[key] = metric
        if cond and counts[key] >= DEBOUNCE and not fired.get(key):
            msgs.append("🔴 " + problem)
            fired[key] = True
            since[key] = time.time()
        elif not cond and fired.get(key):
            bits = []
            if since.get(key):
                bits.append(dur_short(time.time() - since[key]))
            w = worst.get(key)
            if w is not None and key in WORST_FMT:
                bits.append(WORST_FMT[key](w))
            msgs.append("✅ " + recovered + (f" ({', '.join(bits)})" if bits else ""))
            fired[key] = False
        if not cond:                       # episode over, whether or not it fired
            since.pop(key, None)
            worst.pop(key, None)
        if fired.get(key):
            active.append(key)
    state["_counts"], state["_fired"] = counts, fired
    state["_since"], state["_worst"] = since, worst
    STATE.write_text(json.dumps(state))
    for m in msgs:
        telegram(f"🎛️ {m}")          # host is appended by telegram() for every sender
    return active

CSV_METHOD_MARK = "methodology-change-2026-08-07-viewers-require-segments"


def csv_provenance_marker():
    """Write a one-off marker row where the meaning of column 2 changed.

    viewers.csv is kept indefinitely as anonymised statistics and may end up
    cited, so a silent redefinition of a column is the dangerous kind of
    change: rows either side look identical and average together happily.
    This is the same trap as the Phase 5 segment-duration table measured
    through a player that no longer exists - the numbers are not wrong, they
    just answer a question nobody can reconstruct later.

    Idempotent: keyed on the marker string, so restarts do not stack copies.
    """
    try:
        if not CSV.exists():
            return
        body = CSV.read_text()
        if CSV_METHOD_MARK in body:
            return
        # Only a file that actually CONTAINS pre-change rows may be stamped.
        # Without this test a fresh deployment gets marked on its SECOND boot:
        # first boot has no file so nothing is written, by the second the file
        # exists full of new-format rows, and the marker would then assert a
        # methodology change that never happened to them. A provenance line
        # that can be wrong is worse than none, because it is trusted.
        # Old rows are the 5-field ones; the new writer emits 6.
        old = any(len(l.split(",")) == 5
                  for l in body.splitlines()
                  if l and not l.lstrip().startswith("#"))
        if not old:
            return
        with open(CSV, "a") as f:
            f.write(
                f"# {CSV_METHOD_MARK}\n"
                "# ABOVE this line column 2 counted any client requesting any\n"
                "# /dash/ path, manifest polls included. An idle browser tab or a\n"
                "# headless client therefore counted as audience for as long as it\n"
                "# stayed open (measured 2026-08-07: one VPS client held a viewer\n"
                "# slot for 5.5 h on 15 s of actual playback).\n"
                "# BELOW this line column 2 counts only clients fetching\n"
                "# chunk-stream media segments, i.e. actually playing.\n"
                "# The two populations are NOT comparable. Do not average across\n"
                "# this line without saying which side a figure came from.\n"
                "# Column 6 also starts below: renderer starts, a rolling 24 h\n"
                "# gauge of clients whose binaural decode chain initialised. It is\n"
                "# a gauge, not an instantaneous count - do not plot it beside\n"
                "# column 2 on one axis.\n")
    except Exception:
        pass          # provenance is important, but not worth refusing to boot


def history():
    out = []
    try:
        for l in CSV.read_text().splitlines()[-180:]:
            # '#' rows are provenance markers (methodology changes), not data.
            # Skipping them explicitly matters more than it looks: the parse
            # below is wrapped in ONE try, so a single row that raises drops
            # the whole history rather than itself, and a marker containing
            # commas would do exactly that.
            if not l or l.lstrip().startswith("#"):
                continue
            p = l.split(",")
            if len(p) >= 3:
                out.append({"t": p[0],
                            # empty means the viewer probe failed that cycle,
                            # which is not an audience of zero. int("") would
                            # raise and the outer except would drop the entire
                            # history, not just the one row.
                            "v": (int(p[1]) if p[1] not in ("", "None") else None),
                            "temp": (float(p[2]) if p[2] not in ("", "None") else None),
                            # field 4 has always been written and never read.
                            # With on-demand idling it answers the new question:
                            # what fraction of the window was the box encoding?
                            "live": (int(p[3]) if len(p) > 3 and p[3] in ("0", "1") else None)})
    except Exception:
        pass
    return out

def collect_once():
    ps = docker_ps()
    # services() gets its own -a listing rather than reusing `ps`: everything
    # else here (viewers, the source-container lookups) means RUNNING when it
    # says containers, and widening the shared list would quietly change those.
    svcs = services(docker_ps(include_stopped=True))
    strm = stream_state()
    fmt = stream_format()
    per_viewer = (fmt["video_bitrate"] or 0) + (fmt["audio_bitrate"] or 0)   # what ONE client pulls
    vw = viewers(ps)
    # hashed and folded into the rolling gauge immediately; the raw addresses
    # go no further than this line
    rnd = {"starts_24h": renderer_sessions(vw.pop("ir_ips", [])),
           "window_s": IR_WINDOW_S}
    s = {
        "ts": now_iso(), "host": HOST, "services": svcs,
        "all_healthy": all(x["healthy"] for x in svcs) if svcs else False,
        "stream": strm, "encoder": encoder(ps, strm["publishing"]),
        "viewers": vw, "renderer": rnd, "bitrate": mbps(per_viewer or None), **fmt,
        # per-viewer x clients = server load. Zero viewers on a live stream is a
        # real zero; no stream at all is unknown.
        # vw["now"] is None when the viewer probe failed, and None would raise
        # here rather than degrade, taking the whole collect cycle with it
        "egress": mbps(per_viewer * vw["now"]
                       if (per_viewer and vw["now"] is not None) else None),
        "system": {"temp_c": temp_c(), "load1": load1(), "mem_used_pct": mem_pct(),
                   "disk_used_pct": disk_pct(), "uptime_s": uptime_s(),
                   "cores": os.cpu_count()},
    }
    _note_live(strm["live"])            # keep readiness tracked even if nobody polls /api/live
    s["on_demand"] = IDLE_STOP_MIN > 0
    s["idle_stop_min"] = IDLE_STOP_MIN
    # via source_container, not container_named: a one-off `compose run` pusher
    # (pipeline/guest tests) also carries the loop-source service label and
    # would read as the source running while the real one is stopped
    s["source_running"] = bool(source_container(running_only=True))
    tn = tunnel_probe()
    if tn is not None:
        s["tunnel"] = tn
    vp = vod_origin_probe()
    if vp is not None:
        s["vod_origin"] = vp
    ca = cf_vod_analytics()
    if ca is not None:
        s["vod_analytics"] = ca
        # persist for future analysis: one gauge row per FRESH poll (the
        # 5-min cache returns the same 'checked' between polls; stale rows
        # are skipped). Aggregate counts only, no personal data, keep forever
        # like viewers.csv. Columns: checked_ts, views_24h, visits_24h, cc:n;...
        # This belongs to `ca`, not to the backup block below. It was written
        # here (d557688), then df7682d inserted the backup check ABOVE it and
        # captured it into that block, which made the write depend on an
        # unrelated env var: no VOD row was persisted at all unless
        # BACKUP_MARKER happened to be set, and where it WAS set a Cloudflare
        # outage turned ca into None and raised AttributeError on every cycle.
        if not ca.get("stale"):
            try:
                last = ""
                if VODCSV.exists():
                    last = VODCSV.read_text().rsplit("\n", 2)[-2].split(",", 1)[0]
                if last != ca["checked"]:
                    cs = ";".join(f"{k}:{v}" for k, v in ca["countries"].items())
                    with open(VODCSV, "a") as f:
                        f.write(f'{ca["checked"]},{ca["views"]},{ca["visits"]},{cs}\n')
            except Exception:
                pass
    bk = backup_check()
    if bk is not None:
        s["backup"] = bk
    s["alerts_active"] = evaluate_alerts(s)
    guest_tick()                        # backstop for the grace/cap timers
    owner_tick()                        # backstop for the owner-live latch
    with _guest_lock:
        if _guest["state"] == "live":
            v = vw["now"]
            _guest_view["peak"] = max(_guest_view["peak"], v)
            _guest_view["sum"] += v
            _guest_view["n"] += 1
    _guest_log_expire_ips()             # 30-day IP redaction; rows stay as stats
    _bans_expire()                      # same event: ban expiry = IP redaction
    ep = guest_public()
    if ep:
        s["endpoint"] = {**ep, "addr": _guest.get("addr")}   # addr: private page only
        s["bans"] = _bans_lists()                            # private page only
    else:
        # Disabled is a STATE the operator needs to see, not an absence. The
        # public status.json still omits the key entirely (below) so a
        # deployment with the feature off leaves no trace of it to the world -
        # that stays deliberate. But on the private dashboard, hiding the row
        # meant an all-green page while every SRT push was being refused, which
        # is exactly how a first-time setup burns an afternoon: OBS reports a
        # bare I/O error, and the one page that could explain it says nothing.
        s["endpoint"] = {"state": "off"}
    auto_idle(strm, vw.get("any", vw["now"]))
    # Read back after the decision so the panel shows the current countdown
    # rather than last cycle's.
    last_seen, accum = idle_state()
    s["last_viewer_s"] = round(time.time() - last_seen) if last_seen else None
    s["idle_stops_in_s"] = (max(0, round(IDLE_STOP_MIN * 60 - accum))
                            if IDLE_STOP_MIN > 0 and strm["publishing"] else None)
    cc = ";".join(f"{k}:{v}" for k, v in s["viewers"].get("countries", {}).items())
    with open(CSV, "a") as f:
        # column 6 appended rather than inserted: history() reads p[0..3] only,
        # so old rows and new rows both parse, and the marker row says when the
        # column starts. cc is ";"-joined, so it can never eat the new field.
        f.write(f"{s['ts']},{s['viewers']['now']},{s['system']['temp_c']},"
                f"{1 if strm['live'] else 0},{cc},{s['renderer']['starts_24h']}\n")
    s["history"] = history()
    tmp = STATS.with_suffix(".json.tmp"); tmp.write_text(json.dumps(s, indent=1)); tmp.replace(STATS)
    # Deliberately no egress here: aggregate server load is an operator metric, and
    # the per-viewer figure is the only bitrate a visitor can act on.
    pub = {"ts": s["ts"], "live": strm["live"], "publishing": strm["publishing"],
           "resolution": fmt["resolution"], "bitrate": mbps(per_viewer or None),
           "video_codec": fmt["video_codec"], "audio_codec": fmt["audio_codec"],
           "audio_channels": fmt["audio_channels"], "spatial_audio": fmt["spatial_audio"],
           "on_demand": s["on_demand"], "idle_stop_min": IDLE_STOP_MIN,
           "source_running": s["source_running"], "viewers": s["viewers"]["now"],
           "countries": s["viewers"].get("countries", {}), "uptime_s": s["system"]["uptime_s"],
           **({"endpoint": ep} if ep else {})}
    _pub_cache[0] = pub               # for out-of-cycle endpoint refreshes
    PUB.write_text(json.dumps(pub))   # in-place: stable inode for the hoast-player bind mount


def serve():
    DATA.mkdir(parents=True, exist_ok=True)
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k): super().__init__(*a, directory=str(DATA), **k)
        def log_message(self, *a): pass

        def end_headers(self):
            # The dashboard is redeployed often and the browser was holding onto
            # an old copy, so changes appeared not to have shipped. stats.json is
            # already fetched with a cache-buster; the HTML was not.
            p = self.path.split("?")[0]
            if p.endswith("/") or p.endswith(".html"):
                self.send_header("Cache-Control", "no-cache, must-revalidate")
            super().end_headers()

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p, _, q = self.path.partition("?")
            if p == "/api/live":
                return self._json(200, live_probe())
            # nginx-rtmp notify callbacks for the guest app, proxied here by
            # rtmp-ingest (notify_method get). The status code IS the answer:
            # 2xx allows the publish / keeps the stream, anything else rejects
            # or drops it.
            # Both /rtmp/ families are rtmp-ingest's callbacks and nobody
            # else's. Refusing a stranger here is not a behaviour change for
            # the real caller, and it closes the alias's standing hole: an
            # unauthenticated /rtmp/owner/notify from any compose peer could
            # latch owner state, locking out every guest and holding the demo
            # loop down. 404, not 403, so an unauthorised prober cannot even
            # confirm the routes exist.
            # rtmp-ingest is the usual caller, but NOT the only legitimate
            # one: srt-gateway polls /rtmp/guest/precheck-snapshot directly,
            # and a gateway with no GUEST_GW_SECRET latches the owner through
            # /rtmp/owner/notify (the upgrade-in-place path). Both are
            # authenticated by the same socket-bound rule the session
            # protocol uses, so admit them here too.
            # Getting this wrong is not theoretical: gating on ingest alone
            # 404'd the gateway's own snapshot poll, its Snapshot went stale,
            # verdict() failed closed, and the SRT guest endpoint refused
            # EVERY caller while a legacy owner streamed on unlatched. Caught
            # by review the same hour, before it reached the box.
            if p.startswith("/rtmp/"):
                _peer = self.client_address[0]
                if not (_ingest_peer_ok(_peer) or _gw_peer_role(_peer)):
                    return self._json(404, {"error": "not found"})
            if p.startswith("/rtmp/guest/"):
                args = urllib.parse.parse_qs(q)
                name = (args.get("name") or [""])[0]
                addr = (args.get("addr") or [""])[0]
                call = (args.get("call") or [""])[0]
                act = p.rsplit("/", 1)[-1]
                if act == "precheck-snapshot":
                    return self._json(200, guest_precheck_snapshot())
                # SRT sessions arrive via srt-gateway, so nginx-rtmp reports
                # the gateway's address; the real caller rides in ?realip=,
                # honored only under _gw_realip_ok's four conditions so bans,
                # logs and the session record key the actual guest
                realip = (args.get("realip") or [""])[0]
                gwtok = (args.get("gw") or [""])[0]
                if realip and _gw_realip_ok(addr, gwtok, realip):
                    addr = realip
                if act == "publish":
                    return self._json(guest_publish(name, addr), {})
                if act == "done":
                    return self._json(guest_done(name), {})
                if act == "update":
                    # on_update fires for PLAYERS too (call=update_play); only
                    # a publisher's ping may drive session liveness, or a mere
                    # viewer would keep a dead session alive or get adopted
                    if call and call != "update_publish":
                        return self._json(200, {})
                    return self._json(guest_update(name), {})
            # A /owner publish just passed the key check at rtmp-ingest: an
            # external owner's RTMP_OWNER_KEY (as the stream name or ?token=)
            # or the demo loop's LOOP_SOURCE_KEY token - owner_notify tells
            # them apart by the forwarded name. nginx already fails these
            # open, so the responses here don't gate anything.
            # The DIRECT path's session protocol (guest-direct-dash design).
            # Authenticated by the HTTP PEER ADDRESS plus a mandatory shared
            # secret - never by a request field, which is the whole point:
            # these routes have no nginx in front to vouch for anyone, so the
            # connection itself is the only identity that cannot be forged by
            # its own claimant. Must not be proxied; hoast-player forwards only
            # the three /api routes, so nothing does.
            if p.startswith("/gw/session/"):
                args = urllib.parse.parse_qs(q)
                peer = self.client_address[0]
                role = _gw_session_ok(peer, (args.get("gw") or [""])[0])
                if not role:
                    return self._json(403, {"error": "not a known gateway"})
                act = p.rsplit("/", 1)[-1]
                # The floor applies to BEATS ONLY. They are the repetitive
                # call - the 2026-08-09 flood was a keepalive gone wrong - and
                # throttling them costs nothing because a missed beat is
                # retried by design. claim and done are one-shot lifecycle
                # events and MUST NOT be throttled: a done arrives immediately
                # after the 403 beat that ordered the teardown, so a shared
                # floor rejected it, and the session stayed live with its slot
                # held and a kill flag nobody could act on (caught in testing,
                # 2026-08-09 - the limiter broke the very teardown it was
                # protecting).
                if act == "beat" and not _gw_rate_ok(peer):
                    return self._json(429, {"error": "slow down"})
                if act == "claim":
                    # NOTE the role passed on is the AUTHENTICATED one from
                    # _gw_session_ok (which container connected), never the
                    # role= query parameter. That is the point: the owner
                    # gateway can only ever create owner sessions and the
                    # guest gateway only guest ones, so a compromised guest
                    # gateway cannot claim the owner's standing. role= rides
                    # along only because it makes the gateway's own logs
                    # readable.
                    code, body = gw_claim(
                        role,
                        _guest_sanitize((args.get("name") or [""])[0]),
                        (args.get("ip") or [""])[0],
                        (args.get("tracks") or [""])[0])
                    return self._json(code, body)
                if act == "beat":
                    code, body = gw_beat(role, (args.get("session") or [""])[0])
                    return self._json(code, body)
                if act == "done":
                    code, body = gw_done(role, (args.get("session") or [""])[0])
                    return self._json(code, body)
                if act == "reject":
                    code, body = gw_reject(role, (args.get("reason") or [""])[0])
                    return self._json(code, body)
                return self._json(404, {"error": "not found"})
            if p == "/rtmp/owner/notify":
                args = urllib.parse.parse_qs(q)
                owner_notify((args.get("name") or [None])[0],
                             (args.get("addr") or [None])[0])
                return self._json(200, {})
            if p == "/rtmp/owner/done":
                args = urllib.parse.parse_qs(q)
                owner_done((args.get("name") or [None])[0])
                return self._json(200, {})
            return super().do_GET()

        def do_POST(self):
            p = self.path.split("?")[0]
            if p == "/api/start":
                return self._json(200, source_start())
            # /api/stop is reachable only on this port, which is bound to
            # localhost/VPN. It is never proxied to the public player, because
            # stopping is the one verb a visitor could use to ruin the demo for
            # everyone else.
            if p == "/api/stop":
                return self._json(200, source_stop("manual"))
            # End the current guest session (dashboard button). Same trust
            # boundary as /api/stop: this port only.
            if p == "/api/guest/kill":
                return self._json(200, guest_kill())
            if p == "/api/guest/ban":
                return self._json(200 if GUEST_ENABLED else 404, guest_ban())
            if p == "/api/guest/unban":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._json(200 if GUEST_ENABLED else 404,
                                  guest_unban((q.get("ip") or [""])[0]))
            # Abuse report, proxied from the public player with the real
            # reporter identity in headers (nginx cf-aware maps)
            if p == "/api/guest/report":
                code, body = guest_report(
                    self.headers.get("X-Viewer-IP", self.client_address[0]),
                    self.headers.get("X-Viewer-CC", ""))
                return self._json(code, body)
            # on-demand reachability re-check (dashboard button): runs both
            # probes now and returns fresh results without waiting a cycle
            if p == "/api/probe":
                return self._json(200, {"tunnel": tunnel_probe(),
                                        "vod_origin": vod_origin_probe()})
            # Dashboard service restarts (private port only). earshot ships
            # only as the earshot-ingest pair; see RESTARTABLE.
            if p == "/api/restart":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._json(200, restart_services((q.get("svc") or [""])[0]))
            return self._json(404, {"ok": False, "error": "not found"})

    # daemon_threads: a handler thread must never hold the process open at
    # shutdown. The 2026-08-09 flood proved the other half matters more - this
    # server forks one thread per connection with no ceiling, and the
    # container's pids_limit is 256, so a fast enough caller turns "slow" into
    # "telemetry is dead and the alerter with it". request_queue_size lets the
    # kernel hold a burst as backlog instead of spawning threads for it, and
    # _gw_rate_ok refuses the repeat offenders before they take a lock.
    class BoundedServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 64

    with BoundedServer(("", PORT), H) as srv:
        srv.serve_forever()

def main():
    DATA.mkdir(parents=True, exist_ok=True)
    csv_provenance_marker()     # one-off; stamps where the viewer definition changed
    # The dashboard page is baked into the image at /app/web/index.html; expose it in DATA
    src = Path("/app/web/index.html")
    if src.exists():
        html = src.read_text()
        # Optional deployment favicon: the same brand.json the player uses, mounted
        # here read-only. Absent means the committed generic icon stays, so reusers
        # get the default without editing a tracked file.
        try:
            bp = Path(os.environ.get("TEL_BRAND", "/app/brand.json"))
            brand = json.loads(bp.read_text()) if bp.exists() else {}
        except Exception:
            brand = {}
        fav = brand.get("favicon")
        if fav:
            html = re.sub(r'(<link rel="icon" href=")[^"]*(">)',
                          lambda m: m.group(1) + fav + m.group(2), html, count=1)
        # Branded launcher buttons (loop-source row): the link list is baked into
        # the page; the host (and an optional link override) come from the same
        # brand.json, so one file points these buttons at this deployment's
        # address. Absent -> host falls back client-side to the URL reached on.
        if brand.get("host") or brand.get("boxLinks"):
            def _box(m):
                try:
                    box = json.loads(m.group(2))
                except Exception:
                    return m.group(0)
                if brand.get("host"):     box["host"]  = brand["host"]
                if brand.get("boxLinks"): box["links"] = brand["boxLinks"]
                return m.group(1) + json.dumps(box) + m.group(3)
            html = re.sub(r'(window\.__BOX__\s*=\s*)(\{.*\})(;)', _box, html, count=1)
        (DATA/"index.html").write_text(html)
    _guest_boot()      # restore a guest session across telemetry restarts
    threading.Thread(target=_geo_load, daemon=True).start()   # fail-soft geoip
    threading.Thread(target=serve, daemon=True).start()
    while True:
        try:
            collect_once()
        except Exception as e:
            print("collect error:", e, flush=True)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
