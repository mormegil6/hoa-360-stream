#!/usr/bin/env python3
# srt-gateway: privilege-separated SRT terminator for the guest contribution
# leg, and - as a second instance from a deployment override - the owner leg.
#
# Why this exists as its own container: the component that parses hostile
# pre-auth internet bytes (SRT handshakes, mpegts, AAC) must not be the
# component that holds the docker socket (telemetry) or the RTMP_OWNER_KEY +
# public relay (rtmp-ingest). The guest instance holds neither; only the
# separate owner instance carries RTMP_OWNER_KEY (see
# docker-compose.override.yml), which is precisely why it is a second service
# rather than a flag on this one. This process terminates SRT
# with GStreamer's srtsrc - the only tool in the stack that exposes the real
# caller IP and streamid pre-accept (ffmpeg's srt_accept discards both, so
# ban-by-IP would be impossible on an ffmpeg listener) - and republishes each
# session into rtmp-ingest's existing application over one ffmpeg child, so
# every piece of guest arbitration (slot, cap, cooldown, grace, bans, kill,
# stall, demo-loop interlock) is inherited from nginx-rtmp's callbacks
# unchanged. Admission here is a fast pre-filter answered from an in-memory
# snapshot; the authoritative, fail-closed gate remains telemetry's
# on_publish when the child connects.
#
# srtsrc traps learned on the bench, kept load-bearing here:
#   - authentication=true is REQUIRED or caller-connecting never fires and
#     every caller is silently accepted (an inert ban system).
#   - the peer address only has its methods if the Gio typelib is loaded.
#   - a rejected caller's auto-reconnect can retry ~590x/second, so every
#     rejection is memoized for a few seconds and answered without logging
#     or HTTP work per attempt.
import gi
gi.require_version("Gst", "1.0")
gi.require_version("Gio", "2.0")
# Gio must be IMPORTED, not just version-pinned: the typelib loads on import,
# and without it the InetSocketAddress the caller-connecting signal delivers
# has no get_address() method (the exact bench-1 bug, twice now)
from gi.repository import Gst, GLib, Gio  # noqa: F401

import collections
import http.server
import json
import os
import queue
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MODE          = os.environ.get("SRT_MODE", "guest")           # guest | owner
ENABLED       = os.environ.get("SRT_ENABLED", "0") == "1"     # "0" is the
                                                              # bare-process fallback for the var
                                                              # being absent entirely; compose
                                                              # always injects
                                                              # SRT_ENABLED=${SRT_ENABLED:-1}, so
                                                              # the shipped default is ENABLED
LISTEN_PORT   = 8890                                          # container-side, fixed;
                                                              # SRT_PORT only moves the host mapping
LATENCY_MS    = int(os.environ.get("SRT_LATENCY_MS", "2000"))
# Role-specific NAME, not just a role-specific value. One image serves both
# gateways, and until 2026-08-10 both read a shared SRT_PASSPHRASE, which forced
# .env to use a different name (SRT_OWNER_PASSPHRASE) purely so the two roles
# could not collide. That mapping was invisible, had to be explained in four
# documents, and still confused people - `printenv SRT_PASSPHRASE` on the OWNER
# container printing the owner's value is not something anyone should have to be
# told. Reading the role's own name makes the separation structural: the two
# gateways now cannot share a secret because they do not read the same variable,
# and what you set in .env is what you see in the container.
PASSPHRASE_VAR = "SRT_OWNER_PASSPHRASE" if MODE == "owner" else "SRT_GUEST_PASSPHRASE"
PASSPHRASE    = os.environ.get(PASSPHRASE_VAR, "")
# A value left under the retired name would otherwise go quiet, and quiet here is
# a security downgrade: a guest port that was passphrase-protected yesterday
# would come up keyless with nothing said. Refuse instead.
# PRESENCE ONLY, converted at the read: this check needs to know THAT the retired
# name is set, never what it holds, and the value is deliberately not bound to a
# name here. Kept because it is the honest expression of the requirement.
#
# It did NOT silence CodeQL alert #1, and an earlier version of this comment
# claimed it would. The alert points at the string LITERAL below, not at any
# dataflow: the rule matches the word "PASSPHRASE" appearing in a message and
# calls it a logged password. There is nothing to fix, and rewording the error
# so it stops naming the variable would make it useless to the person who has to
# act on it, so the alert is dismissed as a false positive rather than appeased.
_retired_name_set = bool(os.environ.get("SRT_PASSPHRASE"))
if _retired_name_set:
    sys.stderr.write(
        "[srt-gateway] FATAL: SRT_PASSPHRASE was retired on 2026-08-10. Use "
        f"{PASSPHRASE_VAR} instead; leaving the old name set would silently drop "
        "the passphrase on this listener. Re-run scripts/setup.sh to migrate .env.\n")
    sys.exit(1)
STATUS_PORT   = int(os.environ.get("STATUS_PORT", "8091"))
ARBITER_URL   = os.environ.get("ARBITER_URL", "http://telemetry:8090")
INGEST_URL    = os.environ.get("INGEST_URL",
                               "rtmp://rtmp-ingest:1935/guest" if MODE == "guest"
                               else "rtmp://rtmp-ingest:1935/owner")
GW_SECRET     = os.environ.get("GUEST_GW_SECRET", "")
RTMP_OWNER_KEY = os.environ.get("RTMP_OWNER_KEY", "")         # owner mode only
BUFFER_MB     = int(os.environ.get("SRT_BUFFER_MB", "64"))
SNAP_POLL_S   = 2      # snapshot refresh cadence
SNAP_TTL_S    = 10     # older than this = arbiter unreachable = fail closed
CHURN_MEMO_S  = 5      # per-IP rejection memo against reconnect hammering
# The child cannot be spawned until we know whether the caller is sending one
# 4-channel track (1st order) or four (3rd order), because that decides the
# filter graph. Buffer this much of the stream head, probe it, then spawn.
# 1 MB is several mpegts PAT/PMT cycles at any contribution bitrate this
# stack accepts; the wait is the ceiling for a sender that stalls after the
# handshake, and is well inside PUBLISH_BUDGET_S.
PROBE_BYTES    = 1024 * 1024
PROBE_WAIT_S   = 8
PROBE_TIMEOUT_S = 5    # ffprobe itself, on bytes a stranger chose
AUDIO_BITRATE_PER_CH = 96          # kbit/s, the contribution-leg rule
AUDIO_BITRATE = "1536k"   # 96 kbit/s x 16 ch, unused: the 3rd-order path
                          # derives the same figure from AUDIO_BITRATE_PER_CH
PENDING_TTL_S = 5      # handshake accepted but caller-added never arrived: free
                       # the slot rather than let a half-open caller hold it
PUBLISH_BUDGET_S = 20  # a child must become an nginx publisher within this
                       # (covers the ~7.5 s loop handover with margin) or the
                       # session is torn down and the caller memoized - closes
                       # both the probe-hang wedge and the die-and-reloop
JANITOR_S     = 2      # manager-thread maintenance tick when idle
OWNER_MAX_S   = int(os.environ.get("SRT_OWNER_MAX_S", "86400"))
                       # owner sessions bypass the guest arbiter's cap, so the
                       # first hard limit they would meet is the mpegts PTS
                       # wrap (33-bit @ 90 kHz, ~26.5 h), and the demux ->
                       # join -> FLV chain's behavior at that rollover is
                       # unverified. End the session cleanly well before it:
                       # a planned daily reconnect beats an unpredicted
                       # overflow. 0 disables the ceiling. Guest mode never
                       # reads this (telemetry's 3 h cap fires first).
_SECRET_RE    = re.compile(r"\b(gw|token)=[^&\s]+")   # scrub creds from logs
# --- direct-to-DASH handoff (the owner route by default, and guests too when
# GUEST_SRT_DIRECT=1; design in the deployment repo's
# docs/srt-direct-dash-design.md). Instead of the FLV republish - whose forced
# 4x4-AAC -> 16-ch-AAC re-encode measured 59 % of a core at 20 Mbps and costs a
# lossy audio generation - pipe the caller's mpegts to earshot's fixed-layout
# listeners, where earshot's OWN patched ffmpeg (SPD floor, PCE decode, the one
# writer /opt/data/dash tolerates) does join+opus in a single pass. The child
# here becomes a bare -c copy remux, which keeps every existing guard (writer,
# reaper, log scrubber, watchdog wiring) attached to a real process.
# The RTMP chain no longer sees these sessions, so admission and liveness move
# to telemetry's session protocol (/gw/session/claim|beat|done): claim BEFORE
# dialing earshot is the fail-closed gate on_publish used to be, and the beat
# response is the kick lever on_update used to be. GUESTS TAKE THIS PATH TOO
# since 2026-08-09, which is why claim is not optional - a guest that cannot
# be admitted must never reach the listener, and a guest that cannot be
# beaten at must be droppable. The listener enforces the other half (only the
# gateway containers may connect at all; services/earshot/.../
# direct-dash-gate.sh). An older telemetry without the protocol answers 404,
# and only OWNER sessions then fall back to the legacy /rtmp/owner latch.
SRT_DIRECT    = os.environ.get("SRT_DIRECT", "0") == "1"
EARSHOT_HOST  = os.environ.get("EARSHOT_HOST", "earshot")
# (probed track count, probed video codec) -> earshot direct-DASH listener.
# The video codec is part of the key because the listener's -tag:v cannot be
# deferred to ffmpeg: the MP4 muxer rejects the codec tag copied in from
# MPEG-TS, and the value that has to overwrite it differs per codec (avc1 for
# H.264, hvc1 for H.265). Sending H.265 to an avc1 listener produces a muxer
# refusal in a container log the pusher cannot read, which is why the pair is
# resolved here, where a refusal can still be explained.
DIRECT_PORTS  = {(4, "h264"): 9100, (1, "h264"): 9101,
                 (4, "hevc"): 9102, (1, "hevc"): 9103}
DIRECT_NOTIFY_S = 30                    # legacy /rtmp/owner latch re-notify
DIRECT_BEAT_S   = 10                    # session-protocol beat; matches the
                                        # RTMP path's on_update cadence
CLAIM_TIMEOUT_S = 20                    # telemetry holds a claim through the
                                        # demo-loop handover (docker stop plus
                                        # the earshot unwind), so this must
                                        # exceed that whole budget or a real
                                        # admission reads as a timeout
CLAIM_RETRY_S   = 2                     # fixed wall-clock 409 retry
CLAIM_BUDGET_S  = 40                    # total, bounded under the SRT buffer

_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        print(msg, flush=True)


def sanitize(name):
    """Same rule as telemetry's _guest_sanitize: the streamid becomes a
    publish name inside an RTMP URL, so it must never carry URL syntax."""
    name = re.sub(r"[^A-Za-z0-9_-]", "", name or "")[:32]
    return name or "guest"


def parse_streamid(sid):
    """Bare name, or Haivision access-control form '#!::r=name,m=publish'."""
    sid = sid or ""
    if sid.startswith("#!::"):
        for part in sid[4:].split(","):
            k, _, v = part.partition("=")
            if k == "r":
                return v
        return ""
    return sid


def build_join_map():
    """Positional 4x4 -> hexadecagonal channel-name map, read from this
    ffmpeg's own layout table exactly as scripts/merge-obs-tracks.sh does:
    merged channel g IS track t's channel o, never a semantic reorder."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-layouts"],
                         capture_output=True, text=True).stdout
    names = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[0] == "hexadecagonal":
            names = cols[1].split("+")
    if len(names) != 16:
        raise RuntimeError("ffmpeg -layouts lacks a 16-ch hexadecagonal layout")
    entries = [f"{g // 4}.{g % 4}-{names[g]}" for g in range(16)]
    return "|".join(entries)


JOIN_MAP = None  # built once at startup when enabled


def child_command(name, ip, tracks, vcodec="h264"):
    """One session = one ffmpeg: demux the caller's mpegts, put the audio into
    a NAMED layout the AAC encoder can write a PCE for, copy video, and publish
    FLV into rtmp-ingest. The gw secret + realip args are how telemetry
    attributes the session to the real caller instead of this container's
    address (it honors them only from this service's resolved address, with a
    constant-time compare).

    `tracks` is how many 4-channel audio tracks the caller is sending, which
    decides the shape:

      4 -> 3rd order. Join them positionally into one 16-channel
           `hexadecagonal` stream. Merged channel g IS track t's channel o;
           never a semantic reorder.
      1 -> 1st order. The single track is already `quad`, itself a named
           layout, so there is nothing to join and no filter at all.

    Both land on a layout the RTMP leg accepts. Nothing between them does:
    see docs/AMBISONIC-ORDER.md for why 2nd order has to be padded to 16 by
    the sender."""
    if SRT_DIRECT:
        # Bare remux to earshot's listener: no filter, no audio codec, no FLV.
        # The listener's ffmpeg owns the join and the opus encode.
        # MPEGTS on the wire, deliberately: TS is built for joining mid-stream
        # (fixed 188-byte packets, PAT/PMT repeated continuously), which is
        # exactly what an SRT session's first bytes look like. A matroska wire
        # was tried first and abandoned: mkv needs global extradata at header
        # time, real SRT heads can start mid-ADTS-frame, and the resulting
        # "Error parsing AAC extradata" killed sessions that TS shrugs off.
        # The one cost of TS - the mp4 muxer rejecting the copied stream-type
        # codec tag - is paid on the LISTENER side with -tag:v avc1.
        return ["ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-f", "mpegts", "-i", "pipe:0",
                "-map", "0", "-c", "copy",
                "-f", "mpegts", f"tcp://{EARSHOT_HOST}:{DIRECT_PORTS[(tracks, vcodec)]}"]
    if MODE == "guest":
        target = f"{INGEST_URL}/{name}?realip={ip}&gw={GW_SECRET}"
    else:
        target = f"{INGEST_URL}/{name}?token={RTMP_OWNER_KEY}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
           "-analyzeduration", "10M", "-probesize", "20M",
           "-f", "mpegts", "-i", "pipe:0"]
    if tracks == 1:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        fc = (f"[0:a:0][0:a:1][0:a:2][0:a:3]"
              f"join=inputs=4:channel_layout=hexadecagonal:map={JOIN_MAP}[a]")
        cmd += ["-filter_complex", fc, "-map", "0:v:0", "-map", "[a]"]
    # 96 kbit/s per channel, so a 4-channel 1st-order push gets 384k rather
    # than the 1536k sized for 16 channels. Passing the 16-ch figure to a
    # 4-ch encode would spend four times the rate the rule asks for on the
    # contribution leg, which is the one hop where bitrate is paid for good.
    bitrate = f"{AUDIO_BITRATE_PER_CH * 4 * tracks}k"
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", bitrate, "-ar", "48000",
            "-f", "flv", target]
    return cmd


def probe_video_codec(head):
    """Which video codec is in this mpegts head? An ffmpeg codec_name ('h264',
    'hevc', ...), or '' when there is no video stream or it cannot be resolved
    from these bytes.

    A SEPARATE ffprobe pass rather than widening probe_audio_tracks: that one's
    stream counting carries the mid-stream-join semantics documented above, and
    a second read of an already buffered head costs nothing worth entangling
    them for.

    Note what is NOT reachable here. MPEG-TS has no stream type for VP9 or AV1,
    so ffmpeg muxes them as private data and this returns '' (verified
    2026-09-07: a VP9 mpegts probes as codec_type 'data', codec_name
    'bin_data'). Those codecs cannot arrive over this leg at all, which is a
    transport limit, not a policy one."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-f", "mpegts", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "json", "-i", "pipe:0"],
            input=head, capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    try:
        streams = json.loads(r.stdout.decode("utf-8", "ignore")).get("streams", [])
    except ValueError:
        return ""
    return streams[0].get("codec_name", "") if streams else ""


def probe_audio_tracks(head):
    """How many audio tracks, and how many channels each, are in this mpegts
    head? Returns (tracks, channels_per_track, rows), where rows counts the
    audio streams the PMT declares even when their channel count could not be
    resolved. ffprobe reads the same bytes the child would, from a pipe, so a
    partial final packet is harmless.

    rows > 0 with tracks == 0 is the signature of a MID-STREAM join: quad and
    hexadecagonal AAC have no standard ADTS channel_configuration, so the
    channel count lives only in the PCE at the very head of the stream. A
    caller that reconnects without restarting its encoder never resends it,
    and its audio is genuinely undecodable from that point on - not merely
    unprobeable (verified offline 2026-08-09: mid-stream slices of the same
    mux probe as 4 rows x 0 ch at 1 MB and at 4 MB alike, while slices from
    byte 0 probe as 4x4)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-f", "mpegts", "-select_streams", "a",
             "-show_entries", "stream=channels", "-of", "json", "-i", "pipe:0"],
            input=head, capture_output=True, timeout=PROBE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return 0, 0, 0
    # JSON, not csv: with a program in the mpegts, csv output lists every
    # stream TWICE (once under the program, once at top level, separated by a
    # blank line), which reads as 8 tracks for a normal 4-track push and would
    # reject it. The json writer emits one `streams` array.
    try:
        streams = json.loads(r.stdout.decode("utf-8", "ignore")).get("streams", [])
    except ValueError:
        return 0, 0, 0
    counts = [s.get("channels") for s in streams if s.get("channels")]
    if not counts:
        return 0, 0, len(streams)
    return len(counts), counts[0], len(streams)


class Snapshot:
    """Telemetry admission state, polled in the background so the SRT accept
    thread never blocks on HTTP. Stale (arbiter unreachable) = deny: the
    handshake pre-filter fails closed, same contract as on_publish itself."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = None
        self._at = 0.0

    def poll_forever(self):
        url = f"{ARBITER_URL}/rtmp/guest/precheck-snapshot"
        while True:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    data = json.loads(r.read())
                with self._lock:
                    self._data, self._at = data, time.time()
            except Exception:
                pass    # snapshot just goes stale; verdict() denies on TTL
            time.sleep(SNAP_POLL_S)

    def verdict(self, ip):
        """(accept, reason) for a caller at ip, from memory only."""
        with self._lock:
            data, at = self._data, self._at
        if not data or time.time() - at > SNAP_TTL_S:
            return False, "arbiter unreachable (fail closed)"
        if not data.get("enabled"):
            return False, "guest endpoint disabled"
        if ip in data.get("bans", []):
            return False, "banned"
        if not data.get("available"):
            return False, "slot busy or cooling down"
        grace_addr = data.get("grace_addr")
        if grace_addr and ip != grace_addr:
            return False, "slot in another caller's grace window"
        return True, ""


class Gateway:
    # Single-slot state machine, all transitions under self.lock:
    #   free    -> pending  (on_connecting accepts; slot_busy set atomically)
    #   pending -> active   (caller-added -> start_session spawns the child)
    #   *       -> free     (reset_locked, from end_session / janitor / drops)
    # slot_busy is the ONE atomic gate on_connecting checks, so the slot is
    # never observably free between accept and an active session even while
    # Popen runs - which is the race a second caller used to slip through.
    def __init__(self):
        self.snapshot = Snapshot()
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.slot_busy = False       # the atomic admission gate
        self.session = None          # active session dict, or None
        self.pending = None          # (ip, name, at) accepted, awaiting added
        self.memo = {}               # ip -> reject-memo expiry
        self.pipeline = None
        self.pipeline_error = None   # set by a bus ERROR (e.g. UDP bind fail)
        self.started = time.time()

    def _reset_locked(self):
        self.slot_busy = False
        self.session = None
        self.pending = None

    def _drop_caller(self):
        """srtsrc has no per-caller close; cycling the pipeline drops whoever
        is connected and keep-listening re-arms it for the next handshake."""
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _memoize(self, ip):
        now = time.time()
        self.memo[ip] = now + CHURN_MEMO_S
        if len(self.memo) > 4096:
            self.memo = {k: v for k, v in self.memo.items() if v > now}

    # ---- srtsrc signal handlers (streaming threads: memory-only, no I/O) ----

    def on_connecting(self, element, addr, streamid):
        ip = addr.get_address().to_string()
        now = time.time()
        if self.memo.get(ip, 0) > now:
            return False
        # claim-or-reject in ONE critical section so two callers cannot both
        # pass the busy check (verdict reads the snapshot's own lock, taken
        # inside this one, never the reverse - no lock-order inversion)
        with self.lock:
            if self.slot_busy:
                reason = "session already active"
            elif MODE == "guest":
                ok, reason = self.snapshot.verdict(ip)
                reason = "" if ok else reason
            else:
                reason = ""     # owner: single-slot only, passphrase-gated
            if not reason:
                name = sanitize(parse_streamid(streamid))
                self.slot_busy = True
                self.pending = (ip, name, now)
        if reason:
            self._memoize(ip)
            log(f"reject {ip} ({reason})")
            return False
        log(f"accept {ip} streamid={streamid!r} -> name={name}")
        return True

    def on_added(self, element, sock_id, addr):
        ip = addr.get_address().to_string()
        self.events.put(("added", ip, sock_id))

    def on_removed(self, element, sock_id, addr):
        self.events.put(("removed", sock_id))

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        with self.lock:
            s = self.session
        if s is None or s["closing"]:
            return Gst.FlowReturn.OK    # pre/post-session bytes; TS rejoins anywhere
        with s["cond"]:
            s["buf"].append(data)
            s["bytes"] += len(data)
            over = (s["bytes"] > BUFFER_MB * 1024 * 1024 and not s["overflowed"])
            if over:
                s["overflowed"] = True    # one teardown event, not one per sample
            s["cond"].notify()
        if over:
            self.events.put(("force_teardown", "buffer overflow"))
        return Gst.FlowReturn.OK

    # ---- session lifecycle (manager thread, serialized) ----

    def start_session(self, ip, sock_id):
        with self.lock:
            pend = self.pending
            if not self.slot_busy or not pend or pend[0] != ip:
                # a stale caller-added (its accept lost/expired): drop it,
                # leave any legitimate current slot untouched
                self._drop_caller()
                return
            name = pend[1]
        # confirmed starts True in owner mode: _confirm_published can only see
        # the GUEST endpoint via the arbiter, so an owner session could never
        # confirm - the watchdog would tear it down at PUBLISH_BUDGET_S and
        # memoize the caller, a 20 s-on / 5 s-off sawtooth. It also keeps
        # child_exit's early-death memo from punishing an owner whose relay
        # dies at startup (an immediate manual retry should just work).
        # child is spawned by _writer once it has probed the stream head, so
        # everything that touches s["child"] either starts after that or
        # tolerates None (a session can be torn down while still probing).
        s = {"name": name, "ip": ip, "sock_id": sock_id, "child": None,
             "buf": collections.deque(), "bytes": 0,
             "cond": threading.Condition(), "closing": False,
             "overflowed": False, "confirmed": MODE == "owner",
             "since": time.time()}
        with self.lock:
            self.pending = None
            self.session = s
        threading.Thread(target=self._writer, args=(s,), daemon=True).start()
        if MODE == "guest":
            # the watchdog exists to catch a child that never becomes an
            # ARBITER-VISIBLE publisher; the owner has no arbiter view (its
            # /auth is fail-open and /api/live only reports guests), so for
            # owner the confirm can never pass and the thread must not run
            threading.Thread(target=self._watchdog, args=(s,), daemon=True).start()
        log(f"session start: {name} from {ip} (probing stream head)")

    def _spawn_after_probe(self, s):
        """Collect the stream head, ask ffprobe what is in it, and spawn the
        child that matches. Returns the head to replay, or None if the session
        was ended or the caller is sending something this gateway cannot join.

        The probe reads only what the child would have read anyway, so it adds
        no new class of exposure to this process; it just reads it first."""
        head, deadline = bytearray(), time.time() + PROBE_WAIT_S
        while True:
            with s["cond"]:
                while not s["buf"] and not s["closing"] and time.time() < deadline:
                    s["cond"].wait(timeout=0.25)
                if s["closing"]:
                    return None
                while s["buf"]:
                    chunk = s["buf"].popleft()
                    s["bytes"] -= len(chunk)
                    head += chunk
            if len(head) >= PROBE_BYTES or time.time() >= deadline:
                break
        if not head:
            # memoize as every other caller-blaming teardown does (on_connecting,
            # the spawn failure below, _watchdog, child_exit). Without it a
            # sender that never sends media can re-claim the single slot the
            # instant it is freed, looping every PROBE_WAIT_S with no backoff.
            self._memoize(s["ip"])
            self.events.put(("force_teardown", "no media after handshake"))
            return None

        tracks, channels, rows = probe_audio_tracks(bytes(head))
        if tracks == 0:
            # The retry exists for a FRESH head cut short: at contribution
            # bitrates 1 MB is only ~0.4 s and video-dominant muxing can leave
            # the first audio frames (and with them the PCE) still in flight.
            # It cannot rescue a true MID-STREAM join - a bridge reconnecting
            # without restarting its encoder - because the PCE the channel
            # count lives in is only ever at the stream head (see
            # probe_audio_tracks); there the second probe fails identically
            # and the reject below says why. Real encoders (OBS) restart on
            # reconnect and always present a fresh head.
            what = (f"{rows} audio track(s) declared but no channel info"
                    if rows else "no audio")
            log(f"probe: {what} in {len(head)} bytes from {s['ip']}; "
                f"buffering more for one retry")
            deadline = time.time() + PROBE_WAIT_S
            while len(head) < PROBE_BYTES * 4 and time.time() < deadline:
                with s["cond"]:
                    if not s["buf"] and not s["closing"]:
                        s["cond"].wait(timeout=0.25)
                    if s["closing"]:
                        return None
                    while s["buf"]:
                        chunk = s["buf"].popleft()
                        s["bytes"] -= len(chunk)
                        head += chunk
            tracks, channels, rows = probe_audio_tracks(bytes(head))
        # 4x4 is the documented multitrack recipe; 1x4 is a 1st-order source.
        # Anything else cannot be put into a named AAC layout here, and saying
        # so now is kinder than letting the transcode stall for 45 s.
        if tracks == 4 and channels == 4:
            order = "3rd"
        elif tracks == 1 and channels == 4:
            order = "1st"
        else:
            if tracks == 0 and rows:
                why = (f"{rows} audio track(s) but channel count never "
                       f"resolved - mid-stream join without the stream-head "
                       f"PCE? restart the encoder and reconnect")
            else:
                why = (f"unusable audio: {tracks} track(s) x {channels} ch; "
                       f"expected 4x4 or 1x4")
            log(f"reject {s['ip']} ({why})")
            self._memoize(s["ip"])      # same reason as above: no free retry loop
            # Tell the arbiter WHY, out of band. This refusal happens before
            # _session_claim, so there is no session for done to reference, and
            # SRT cannot explain a teardown to an established caller anyway: the
            # sender just sees the connection drop. Without this the gateway is
            # the only thing that ever knows, in a container log the pusher
            # cannot read, which cost a tester an evening of guessing.
            self._notify_reject(why)
            self.events.put(("force_teardown",
                             f"unsupported audio layout ({tracks}x{channels})"))
            return None

        # Video codec, same deal one layer down: it decides the listener port on
        # the direct path and the muxer on the legacy one, so an unusable codec
        # has to be refused HERE, the last place that can still say why. The
        # listener has no route to telemetry, so a codec it cannot tag dies in a
        # container log the pusher will never read - which is exactly how an
        # H.265 push failed for a tester in September 2026.
        #
        # Refuse only on a POSITIVE identification. An empty probe means these
        # bytes did not resolve a video stream, not that the stream is bad, and
        # turning an unlucky probe into a refused session would be a worse bug
        # than the one being fixed; fall back to the historical H.264 assumption
        # and let the muxer be the judge, exactly as before.
        probed_v = probe_video_codec(bytes(head))
        vcodec = probed_v or "h264"
        # H.265 needs the direct path: the legacy leg republishes over FLV,
        # which has no HEVC stream type at all.
        allowed = ("h264", "hevc") if SRT_DIRECT else ("h264",)
        if probed_v and vcodec not in allowed:
            # Named the way an encoder's UI names them, not the way ffmpeg does:
            # this sentence is read by whoever is trying to push.
            pretty = {"h264": "H.264", "hevc": "H.265"}
            why = (f"unsupported video codec '{vcodec}'; this deployment "
                   f"carries {' or '.join(pretty[c] for c in allowed)}")
            log(f"reject {s['ip']} ({why})")
            self._memoize(s["ip"])
            self._notify_reject(why)
            self.events.put(("force_teardown",
                             f"unsupported video codec ({vcodec})"))
            return None

        # The session can have been torn down while we were in ffprobe (the
        # caller dropping is the common way). Spawning then would leave an
        # ffmpeg the gateway no longer tracks, briefly publishing the buffered
        # head as a caller who has already gone, which makes telemetry open a
        # grace window for a session that never legitimately started. Check
        # under the lock, and check AGAIN after the spawn, because a teardown
        # can land in between; on that path kill what we just started.
        with self.lock:
            if self.session is not s or s["closing"]:
                return None
        # CLAIM BEFORE CONNECT, and the ordering is the whole point: on the
        # direct path telemetry's answer here is the fail-closed admission
        # gate that nginx-rtmp's on_publish is on the RTMP path. No 200, no
        # bytes to earshot. Doing it after the spawn would put an unadmitted
        # writer on the single-writer DASH tree for as long as the round trip
        # takes, which is exactly the hole the design review called BLOCKER 2's
        # sibling. Owner sessions claim too: it is how telemetry latches the
        # owner, locks guests out and stops the demo loop.
        if SRT_DIRECT:
            if not self._session_claim(s, tracks):
                return None
        try:
            child = subprocess.Popen(child_command(s["name"], s["ip"], tracks, vcodec),
                                     stdin=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        except OSError as e:
            log(f"spawn failed for {s['name']} from {s['ip']}: {e}")
            self._memoize(s["ip"])
            self.events.put(("force_teardown", "spawn failed"))
            return None

        with self.lock:
            still_current = self.session is s and not s["closing"]
            if still_current:
                s["child"] = child      # published under the same lock
                                        # _terminate_child reads it under
        if not still_current:
            log(f"session ended while probing; discarding child pid {child.pid}")
            try:
                child.kill()
                child.stdin.close()
            except OSError:
                pass
            return None
        threading.Thread(target=self._child_log, args=(s,), daemon=True).start()
        threading.Thread(target=self._reaper, args=(s,), daemon=True).start()
        if SRT_DIRECT:
            threading.Thread(target=self._direct_latch, args=(s,), daemon=True).start()
        log(f"session media: {s['name']} from {s['ip']} - {tracks} x {channels} ch "
            f"({order} order, {vcodec} video, child pid {child.pid}"
            f"{', DIRECT to earshot:' + str(DIRECT_PORTS[(tracks, vcodec)]) if SRT_DIRECT else ''})")
        return bytes(head)

    def _session_claim(self, s, tracks):
        """Ask telemetry to admit this session, BEFORE any byte is sent to
        earshot. True means admitted (and s['session'] now holds the id).

        This is the direct path's fail-closed admission gate - the equivalent
        of nginx-rtmp's on_publish, which does not exist here. It runs the ban
        list, the owner lockout, the single-slot rule, the cooldown, the
        grace-address carve-out and the demo-loop handover, because telemetry
        answers it with the very same code the RTMP path uses.

        409 means 'not yet, retry': the slot could not be cleared inside the
        handover budget. Retried on a fixed wall-clock interval, bounded well
        under the SRT receive buffer's depth so a drain that never settles
        ends the session rather than silently overflowing it.

        A 404 means this telemetry predates the protocol. Owner sessions then
        fall back to the /rtmp/owner alias (legacy mode in the beat loop) so a
        gateway upgraded ahead of telemetry still works; guests do NOT - their
        admission is the thing being asked for, and assuming it would put an
        unadmitted stranger on the DASH tree."""
        # No secret configured here means this gateway CANNOT authenticate to
        # the session protocol, and telemetry would refuse every claim. That
        # must not silently break a working owner route on upgrade (the box
        # ran direct-to-DASH for a day before the protocol existed), so:
        # owner degrades to the legacy /rtmp/owner latch, which is exactly what
        # it used before and no weaker than yesterday; a GUEST is refused,
        # because unauthenticated admission is the one thing that must never
        # degrade open. setup.sh generates the secret, so this is the
        # upgrade-in-place path, not the supported configuration.
        if not GW_SECRET:
            if MODE == "owner":
                log("no GUEST_GW_SECRET: using the legacy /rtmp/owner latch for "
                    "this owner session (set one to use the session protocol)")
                s["session"] = None
                s["tracks"] = tracks
                return True
            log(f"no GUEST_GW_SECRET: refusing the direct path for guest "
                f"{s['name']} from {s['ip']} (cannot authenticate to the arbiter)")
            self._memoize(s["ip"])
            self.events.put(("force_teardown", "gateway cannot authenticate"))
            return False
        deadline = time.time() + CLAIM_BUDGET_S
        while True:
            # The caller can vanish mid-claim (dropped SRT, teardown, an owner
            # preempting us). Registering a session for a caller that is
            # already gone strands the slot until beat-silence notices, which
            # is minutes of dead air on the public test point.
            with self.lock:
                if self.session is not s or s["closing"]:
                    log(f"claim abandoned: session for {s['ip']} ended while "
                        f"waiting for the arbiter")
                    return False
            url = (f"{ARBITER_URL}/gw/session/claim"
                   f"?role={'owner' if MODE == 'owner' else 'guest'}"
                   f"&name={urllib.parse.quote(s['name'])}"
                   f"&ip={urllib.parse.quote(s['ip'])}"
                   f"&tracks={tracks}"
                   f"&gw={urllib.parse.quote(GW_SECRET)}")
            try:
                with urllib.request.urlopen(url, timeout=CLAIM_TIMEOUT_S) as r:
                    body = json.loads(r.read() or b"{}")
                sid = body.get("session")
                # Re-check AFTER the round trip: a 200 that arrives for a dead
                # session must be handed straight back, or telemetry holds a
                # slot for a caller that no longer exists.
                with self.lock:
                    alive = self.session is s and not s["closing"]
                if not alive:
                    log("claim returned after the session ended; releasing it")
                    if sid:
                        try:
                            urllib.request.urlopen(
                                f"{ARBITER_URL}/gw/session/done?session="
                                f"{urllib.parse.quote(sid)}"
                                f"&gw={urllib.parse.quote(GW_SECRET)}",
                                timeout=4).read()
                        except Exception:
                            pass
                    return False
                s["session"] = sid
                s["tracks"] = tracks
                return True
            except urllib.error.HTTPError as e:
                if e.code == 409 and time.time() < deadline:
                    time.sleep(CLAIM_RETRY_S)
                    continue
                if e.code == 404 and MODE == "owner":
                    log("arbiter has no session protocol; using the legacy "
                        "/rtmp/owner latch for this owner session")
                    s["session"] = None
                    s["tracks"] = tracks
                    return True
                log(f"claim refused for {s['name']} from {s['ip']} "
                    f"(HTTP {e.code}); rejecting the caller")
                self._memoize(s["ip"])
                self.events.put(("force_teardown", "not admitted by the arbiter"))
                return False
            except Exception as e:
                # The arbiter is unreachable. Fail CLOSED: admission is the one
                # decision that must never default to yes, and a guest admitted
                # while telemetry is down is a guest nothing can kick.
                if time.time() < deadline:
                    time.sleep(CLAIM_RETRY_S)
                    continue
                log(f"claim unreachable for {s['name']} from {s['ip']}: {e}")
                self._memoize(s["ip"])
                self.events.put(("force_teardown", "arbiter unreachable"))
                return False

    def _direct_latch(self, s):
        """The DIRECT path has no rtmp-ingest in front of it, so nothing
        fires the nginx callbacks telemetry relies on: this thread IS those
        callbacks. It beats every DIRECT_BEAT_S while the session lives and
        sends one done on the way out, both keyed on the session id the claim
        returned.

        THE BEAT RESPONSE IS THE KICK LEVER, and acting on it is what makes an
        operator kill, the session cap, a ban, the resource guard and owner
        preemption reach a direct session at all - on the RTMP path they all
        arrive as a non-2xx answer to nginx's on_update ping, which does not
        exist here. Verdicts:
          403  telemetry has ended this session: terminate and drop the caller
          410  telemetry does not know this session (a redeploy lost it, or it
               was demoted): re-claim with full identity, the direct analogue
               of the RTMP path's stat-backed adoption
          anything else, including an unreachable arbiter: transient, keep
               streaming and retry. A briefly unreachable arbiter must never
               tear down a healthy broadcast; only a DELIBERATE 403 does.

        Wall-clock deadlines, never a wakeup counter: the session cond is
        notified for every arriving SRT packet, so a live stream wakes this
        wait thousands of times a minute. The first version counted each
        wakeup as five elapsed seconds and re-notified at packet rate (~80
        requests/s measured), burying telemetry in handler threads to its pids
        limit within minutes (2026-08-09). Telemetry now refuses that rate
        itself with a 429, but the client stays correct on its own."""
        legacy = not s.get("session")     # older telemetry; see _session_claim

        def beat():
            """Returns 'ok', 'kill', 'reclaim' or 'transient'."""
            if legacy:
                url = (f"{ARBITER_URL}/rtmp/owner/notify"
                       f"?name={urllib.parse.quote(s['name'])}")
            else:
                url = (f"{ARBITER_URL}/gw/session/beat"
                       f"?session={urllib.parse.quote(s['session'])}"
                       f"&gw={urllib.parse.quote(GW_SECRET)}")
            try:
                urllib.request.urlopen(url, timeout=4).read()
                return "ok"
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    return "kill"
                if e.code == 410:
                    return "reclaim"
                if e.code == 404 and legacy:
                    # The legacy latch route is GONE, so this session is
                    # writing DASH that telemetry knows nothing about - the
                    # loop can start on top of it and nothing will ever end
                    # it. That is precisely what the fallback was supposed to
                    # prevent, so it is fatal, not transient.
                    log("legacy /rtmp/owner latch returned 404: this arbiter "
                        "does not accept it; ending the session rather than "
                        "streaming unlatched")
                    return "kill"
                log(f"direct-session beat got {e.code}; treating as transient")
                return "transient"
            except Exception as e:
                log(f"direct-session beat failed (retrying): {e}")
                return "transient"

        if legacy:
            beat()                        # the alias's one-shot notify
        verdict = None
        next_beat = time.time() + (DIRECT_NOTIFY_S if legacy else DIRECT_BEAT_S)
        while True:
            with s["cond"]:
                s["cond"].wait(timeout=5)
                closing = s["closing"]
            with self.lock:
                current = self.session is s
            if closing or not current:
                break
            now = time.time()
            if now >= next_beat:
                r = beat()
                if r == "kill":
                    verdict = "kill"
                    break
                if r == "reclaim" and not legacy:
                    # Idempotent re-introduction. A refusal means the slot is
                    # genuinely gone (preempted, banned, capped): a real
                    # verdict, not a blip, so the session ends.
                    if not self._session_claim(s, s.get("tracks")):
                        verdict = "kill"
                        break
                    log(f"direct-session re-claimed after 410: {s['name']}")
                interval = DIRECT_NOTIFY_S if legacy else DIRECT_BEAT_S
                next_beat = now + (interval if r in ("ok", "reclaim") else 5)
        if verdict == "kill":
            log(f"direct-session ended by the arbiter: {s['name']} from {s['ip']}")
            self.events.put(("force_teardown", "ended by the arbiter"))
        # WAIT FOR THE WRITER TO ACTUALLY DIE before saying done. Telemetry
        # treats done as "the slot is free", and _resume_after_guest starts
        # the demo loop off the back of it - so if done means only "teardown
        # requested", the loop's transcoder can start while earshot's listener
        # is still draining buffered bytes into the same single-writer DASH
        # tree. _terminate_child gives the child up to 8 s before SIGKILL, and
        # the bytes already inside socat and the kernel socket keep being
        # muxed until it exits, so this wait is the difference between "done"
        # being a promise and being a fact.
        child = s.get("child")
        if child is not None:
            try:
                child.wait(timeout=15)
            except Exception:
                log("writer still alive 15 s after teardown; sending done "
                    "anyway so the slot cannot wedge")
        # One done, not the RTMP path's two-six-seconds-apart pair: that hack
        # existed because a name-only done could not be told from a dead
        # predecessor's late callback, so telemetry guessed with a 5 s window
        # and swallowed roughly one legitimate done in six. The session id
        # settles identity outright, so a single done is sufficient and exact.
        if legacy:
            url = f"{ARBITER_URL}/rtmp/owner/done?name={urllib.parse.quote(s['name'])}"
        else:
            # WHY the session ended, not just that it did. Without this the
            # gateway knows exactly what was wrong ("unusable audio: 1 track(s)
            # x 2 ch") and the sender sees only a dropped connection: SRT can
            # carry a rejection reason during the handshake, but the audio
            # layout is not known until media has been buffered and probed, and
            # an established connection has no channel to explain its own
            # teardown. So the reason goes out of band, for the status page the
            # sender is already watching. One publisher at a time means the
            # last reason is unambiguous without identifying the caller, which
            # is just as well: the department's UDP forward presents every
            # external guest as the same source address.
            url = (f"{ARBITER_URL}/gw/session/done"
                   f"?session={urllib.parse.quote(s['session'])}"
                   f"&gw={urllib.parse.quote(GW_SECRET)}"
                   f"&reason={urllib.parse.quote(str(s.get('end_reason') or ''))[:200]}")
        # Retried, because a lost done strands the slot: telemetry would hold
        # this session live until the 60 s beat-silence rule demoted it, with
        # the demo loop still down. Cheap insurance against a transient blip.
        for attempt in range(3):
            try:
                urllib.request.urlopen(url, timeout=4).read()
                break
            except Exception as e:
                log(f"direct-session done failed (attempt {attempt + 1}/3): {e}")
                time.sleep(1.5)
        if legacy:
            time.sleep(6)
            try:
                urllib.request.urlopen(url, timeout=4).read()
            except Exception:
                pass

    def _writer(self, s):
        head = self._spawn_after_probe(s)
        if head is None:
            return
        try:
            s["child"].stdin.write(head)
            while True:
                with s["cond"]:
                    while not s["buf"] and not s["closing"]:
                        s["cond"].wait(timeout=1)
                    if not s["buf"] and s["closing"]:
                        break
                    chunk = s["buf"].popleft()      # O(1); list.pop(0) was O(n)
                    s["bytes"] -= len(chunk)
                s["child"].stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass    # child died; the reaper reports it
        try:
            s["child"].stdin.close()
        except OSError:
            pass

    def _child_log(self, s):
        for line in s["child"].stderr:
            # ffmpeg prints the full output URL on any open failure; the guest
            # target carries ?gw=<secret> (owner: ?token=<RTMP_OWNER_KEY>), so
            # scrub before it reaches docker logs - same care as access_log off
            clean = _SECRET_RE.sub(r"\1=***", line.decode(errors="replace").rstrip())
            log(f"[ffmpeg {s['name']}] {clean}")

    def _reaper(self, s):
        rc = s["child"].wait()
        self.events.put(("child_exit", s, rc))

    def _watchdog(self, s):
        """A child that never becomes an nginx publisher (mpegts that never
        yields a PMT / four audio tracks makes ffmpeg probe forever) would
        wedge the slot invisibly to telemetry. Give it the handover budget,
        then confirm via the arbiter it actually went live; if not, tear down
        and memoize the caller so it cannot immediately re-wedge."""
        time.sleep(PUBLISH_BUDGET_S)
        with self.lock:
            current = self.session is s and not s["closing"]
        if not current:
            return
        if self._confirm_published(s):
            s["confirmed"] = True
            return
        log(f"watchdog: {s['name']} from {s['ip']} never became a publisher")
        with self.lock:
            self._memoize(s["ip"])
        self.events.put(("force_teardown", "never published"))

    def _confirm_published(self, s):
        try:
            with urllib.request.urlopen(f"{ARBITER_URL}/api/live", timeout=3) as r:
                ep = (json.loads(r.read()).get("endpoint") or {})
            return ep.get("name") == s["name"] and ep.get("state") in ("live", "handover")
        except Exception:
            return False

    def _notify_reject(self, why):
        """Best effort: a refusal the pusher should be told about. Never raises
        and never blocks the teardown - the connection is going away either way,
        and a stalled arbiter must not hold the slot open."""
        if MODE != "guest":
            return
        try:
            url = (f"{ARBITER_URL}/gw/session/reject"
                   f"?gw={urllib.parse.quote(GW_SECRET)}"
                   f"&reason={urllib.parse.quote(str(why)[:200])}")
            urllib.request.urlopen(url, timeout=3).read()
        except Exception as e:
            log(f"reject notice not delivered ({e})")

    def end_session(self, s, drop_caller, reason):
        # Set before anything else: _terminate_child runs on its own thread and
        # is what posts done, so recording the reason later would race it.
        s["end_reason"] = reason
        with self.lock:
            if self.session is not s:
                return
            self._reset_locked()
        with s["cond"]:
            s["closing"] = True
            s["cond"].notify()
        if drop_caller:
            self._drop_caller()
        # the bounded wait-then-kill runs off the manager thread so a wedged
        # child cannot stall event processing (a new caller can be admitted at
        # once; this child is already detached from the slot)
        threading.Thread(target=self._terminate_child, args=(s,), daemon=True).start()
        log(f"session end: {s['name']} from {s['ip']} ({reason})")

    def _terminate_child(self, s):
        with self.lock:
            child = s["child"]
        if child is None:
            return      # ended while still probing; _spawn_after_probe's own
                        # post-spawn re-check kills anything started after this
        s["child"].terminate()
        deadline = time.time() + 8      # under docker's 10 s stop grace
        while s["child"].poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if s["child"].poll() is None:
            s["child"].kill()

    def _janitor(self):
        """Idle-tick maintenance: a pending that never advanced to a session
        (caller-added never fired) holds slot_busy - free it after PENDING_TTL
        so a half-open handshake cannot wedge the slot with no new caller to
        trigger the inline expiry."""
        with self.lock:
            pend = self.pending
            stale = (self.slot_busy and self.session is None and pend
                     and time.time() - pend[2] > PENDING_TTL_S)
            if stale:
                self._reset_locked()
        if stale:
            log(f"pending timeout: {pend[1]} from {pend[0]} never connected")
            self._drop_caller()
        # owner session ceiling (see OWNER_MAX_S). The janitor tick is the
        # natural place: it runs every JANITOR_S whenever the event queue is
        # idle, which a healthy long session is. The event carries the
        # SESSION it targets, not just a reason: a bare force_teardown
        # dispatched after this session already ended (its 'removed' can be
        # queued ahead of us) would wrongly drop an innocent reconnect's
        # fresh pending handshake. Teardown drops the caller, no memoize, so
        # an immediate reconnect starts fresh with a fresh PTS epoch.
        if MODE == "owner" and OWNER_MAX_S > 0:
            with self.lock:
                s = self.session
                expired = (s is not None and not s["closing"]
                           and time.time() - s["since"] > OWNER_MAX_S)
            if expired:
                self.events.put(("ceiling_teardown", s,
                                 f"owner session ceiling {OWNER_MAX_S}s "
                                 f"(mpegts PTS-wrap guard); reconnect to continue"))

    def manage_forever(self):
        while True:
            try:
                ev = self.events.get(timeout=JANITOR_S)
            except queue.Empty:
                self._janitor()
                continue
            try:
                self._dispatch(ev)
            except Exception as e:      # one bad event must not kill the only
                                        # session thread and silently wedge all
                log(f"manager error on {ev[0]}: {e!r}")

    def _dispatch(self, ev):
        kind = ev[0]
        with self.lock:
            s = self.session
        if kind == "added":
            _, ip, sock_id = ev
            self.start_session(ip, sock_id)
        elif kind == "removed":
            _, sock_id = ev
            if s and s["sock_id"] == sock_id:
                # caller left; let the child flush and exit so nginx sees a
                # clean publish_done and telemetry opens the grace window
                self.end_session(s, drop_caller=False, reason="caller disconnected")
            elif not s:
                with self.lock:
                    if self.pending:
                        self._reset_locked()
        elif kind == "child_exit":
            _, dead, rc = ev
            if s is dead:
                # relay died under a live caller (kill 403, wrong-shape input,
                # earshot restart): end, never resplice. If it died before it
                # ever published, memoize so a bad input cannot tight-loop.
                if not dead["confirmed"] and time.time() - dead["since"] < PUBLISH_BUDGET_S:
                    with self.lock:
                        self._memoize(dead["ip"])
                self.end_session(s, drop_caller=True, reason=f"relay exited rc={rc}")
        elif kind == "ceiling_teardown":
            _, target, reason = ev
            if s is target:                 # already gone = nothing to do;
                self.end_session(s, drop_caller=True, reason=reason)
        elif kind == "force_teardown":
            if s:
                self.end_session(s, drop_caller=True, reason=ev[1])
            else:
                with self.lock:
                    if self.pending:
                        self._reset_locked()
                self._drop_caller()

    # ---- pipeline + status ----

    def build_pipeline(self):
        desc = (f"srtsrc name=src mode=listener localport={LISTEN_PORT} "
                f"latency={LATENCY_MS} authentication=true keep-listening=true "
                f"wait-for-connection=false ! queue ! "
                f"appsink name=sink emit-signals=true sync=false max-buffers=0")
        self.pipeline = Gst.parse_launch(desc)
        src = self.pipeline.get_by_name("src")
        if PASSPHRASE:
            src.set_property("passphrase", PASSPHRASE)
        src.connect("caller-connecting", self.on_connecting)
        src.connect("caller-added", self.on_added)
        src.connect("caller-removed", self.on_removed)
        self.pipeline.get_by_name("sink").connect("new-sample", self.on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _on_bus_error(self, bus, msg):
        err, _ = msg.parse_error()
        # a fatal pipeline error (typically the UDP bind failing) must show as
        # unhealthy, not a silently-accepting-nobody gateway that reads green
        self.pipeline_error = str(err)
        log(f"pipeline ERROR: {err}")

    def pipeline_ok(self):
        # a live srtsrc listener idles in PAUSED/ASYNC with no caller, so
        # PLAYING is not a health signal; the real failure (UDP bind conflict,
        # element error) arrives as a bus ERROR, latched here
        return self.pipeline is not None and self.pipeline_error is None

    def status(self):
        with self.lock:
            s = self.session
        return {"mode": MODE, "enabled": ENABLED, "transport": "srt",
                "ok": self.pipeline_ok(),
                "error": self.pipeline_error,
                "active": s is not None,
                "name": s["name"] if s else None,
                "addr": s["ip"] if s else None,
                "since": s["since"] if s else None,
                "uptime_s": round(time.time() - self.started)}


def serve_status(gw):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/health":
                # when enabled, health means the SRT pipeline built and has not
                # hit a fatal bus error (e.g. UDP bind conflict) - not merely
                # that this HTTP thread is alive
                ok = (not ENABLED) or gw.pipeline_ok()
                body, code = (b"OK", 200) if ok else (b"pipeline down", 503)
            elif self.path == "/status":
                body, code = json.dumps(gw.status()).encode(), 200
            else:
                body, code = b"not found", 404
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Srv(socketserver.ThreadingTCPServer):
        allow_reuse_address = True      # must be a class attr: set after bind
                                        # (as it was) is a no-op
        daemon_threads = True

    with Srv(("", STATUS_PORT), H) as srv:
        srv.serve_forever()


def main():
    global JOIN_MAP
    gw = Gateway()
    threading.Thread(target=serve_status, args=(gw,), daemon=True).start()
    if not ENABLED:
        # Reached only when SRT_ENABLED=0 is set explicitly: the shipped
        # default is ENABLED (docker-compose.yml injects ${SRT_ENABLED:-1} for
        # this service and for telemetry; see .env.example and the README
        # variable table). Disabled means never bind the SRT port while
        # keeping the health endpoint up, so compose healthchecks stay green.
        # The port alone admits nobody anyway: guest mode still needs
        # GUEST_ENABLED=1.
        log(f"srt-gateway idle (SRT_ENABLED=0); status on :{STATUS_PORT}")
        signal.pause()
        return
    if MODE == "guest" and not GW_SECRET:
        # not fatal: telemetry authenticates this gateway by its container
        # address, which a remote publisher cannot forge. The secret is an
        # extra layer, not the one holding attribution up.
        log("note: GUEST_GW_SECRET unset - caller attribution rests on the "
            "gateway's address alone; set one to add a shared-secret check")
    if MODE == "owner" and not RTMP_OWNER_KEY:
        log("FATAL: owner mode requires RTMP_OWNER_KEY")
        sys.exit(1)
    if MODE == "owner" and not PASSPHRASE:
        # owner mode does no per-caller auth (any completed handshake becomes an
        # owner broadcast: remuxed straight into earshot under the default
        # SRT_DIRECT=1, republished with the real RTMP_OWNER_KEY under
        # SRT_DIRECT=0), so the SRT passphrase is the
        # only thing standing between a public caller and an owner-broadcast
        # takeover. Required, not optional, in this mode.
        log("FATAL: owner mode requires SRT_OWNER_PASSPHRASE (it is the caller gate; "
            "without it any handshake becomes an authenticated owner publish)")
        sys.exit(1)
    Gst.init(None)
    JOIN_MAP = build_join_map()
    if MODE == "guest":
        threading.Thread(target=gw.snapshot.poll_forever, daemon=True).start()
    threading.Thread(target=gw.manage_forever, daemon=True).start()
    gw.build_pipeline()
    log(f"srt-gateway listening on udp/{LISTEN_PORT} "
        f"(mode={MODE}, latency={LATENCY_MS}ms, "
        f"encrypted={'yes' if PASSPHRASE else 'no'})")
    loop = GLib.MainLoop()

    def stop(*_):
        with gw.lock:
            s = gw.session
        if s:
            gw.end_session(s, drop_caller=False, reason="gateway shutdown")
        loop.quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    loop.run()


if __name__ == "__main__":
    main()
