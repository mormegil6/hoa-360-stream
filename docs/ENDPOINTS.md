# Endpoints & ports

Every address the stack exposes, what serves it, and whether it is meant to be public or private. Ports below are the Compose host-published ports; a deployment maps them to real addresses/domains in `docker-compose.override.yml` (the [committed example](../docker-compose.override.yml.example) shows the blocks) and (for anything public) a reverse proxy or tunnel.

## Am I broadcasting to the internet right now?

A fair question to ask before `docker compose up`, and the honest answer has two halves.

**Nothing of yours is visible to anyone else.** The player, the operations dashboard and the Earshot monitor all bind to `127.0.0.1` only, so nothing outside your own machine can watch, and the stack makes no outbound connection to publish anywhere. Making a demo public is a separate, deliberate act: a reverse proxy or a tunnel you set up yourself.

**Three ports do listen on all interfaces**, and it is better to know than to be reassured: `1935/tcp` (RTMP contribution), `8890/udp` (SRT), and `8891/udp` if you ran `scripts/setup.sh`, which writes the owner route. Those are INBOUND - they exist so that you, or a guest you have deliberately enabled, can send a stream IN. Each one is gated: `rtmp-ingest` refuses to start at all while its keys are the placeholders committed to the repository, the owner SRT route is useless without your passphrase, and the guest port admits nobody unless you set `GUEST_ENABLED=1`. They are reachable from your LAN, and from the internet only if you forward them yourself.

The table below is the port-by-port version of the same answer. If you want certainty rather than reasoning, `docker compose down` stops everything.

## Container-published ports

| Port | Service | Serves | Exposure intent |
|---|---|---|---|
| 1935/tcp | `rtmp-ingest` | RTMP contribution `rtmp://<host>:1935/owner/<key>` | public only if you run open ingest; else LAN/VPN |
| 8890/udp | `srt-gateway` | SRT contribution `srt://<host>:8890?streamid=<name>` (native OBS multitrack; one 4-channel track for 1st order, four for 3rd, detected from the stream), the recommended ingest; bound by default, but admits nobody unless `GUEST_ENABLED=1` (`SRT_ENABLED=0` unbinds it) | public if you run the SRT guest endpoint. The gateway is privilege-separated (no docker socket, no volumes, no `RTMP_OWNER_KEY`/`LOOP_SOURCE_KEY`, read-only rootfs) because it terminates hostile pre-auth internet bytes. Only the separate owner instance below carries `RTMP_OWNER_KEY` |
| 8891/udp | `srt-gateway-owner` | SRT contribution for the OPERATOR's own stream: `srt://<box-address>:8891?streamid=owner&passphrase=<SRT_OWNER_PASSPHRASE>`. By default (`SRT_DIRECT=1`) remuxes straight into `earshot`'s direct-DASH listener, bypassing `rtmp-ingest` and the guest arbiter; with `SRT_DIRECT=0` it republishes into the token-authed `owner` application instead. Needs no `GUEST_ENABLED` either way. Not in the base compose: `scripts/setup.sh` writes it into `docker-compose.override.yml` | as public as you bind it. `setup.sh` binds all interfaces, matching 1935 and 8890; the mandatory `SRT_OWNER_PASSPHRASE` is the gate (SRT uses it as the connection's AES key, so a caller without it is refused at the handshake). Narrow the bind to loopback or a VPN address for less exposure. Unlike the guest gateway this instance DOES carry `RTMP_OWNER_KEY`, which is why it is a separate service |
| 8080/tcp | `hoast-player` | player `/`, DASH `/dash/<DASH_NAME>.mpd`, public status `/status/status.json`, `telemetry` proxy `/api/live` (GET), `/api/start` (POST, rate-limited 6r/m burst 3) and `/api/guest/report` (POST, 1r/m burst 3) | **public** (front with TLS / a tunnel) |
| 8081/tcp | `earshot` | dev monitor `/webtools`, `/stat`, `/dash`. On an owner-direct stream the panel discovers the stream from the DASH manifest rather than the RTMP stat page, and labels the resulting `Num Clients 0` as expected rather than a fault | **private**: debug only. `docker-compose.yml` binds `127.0.0.1:8081:80` (loopback only). Note Compose appends port entries, so a plain override list can widen but not narrow a base mapping (narrowing needs `!override`/`!reset` on the key). Firewall the port or edit the base file |
| 8090/tcp | `telemetry` | dashboard `/`, `/stats.json`, `/viewers.csv` | **private**: bind localhost/VPN only, never `0.0.0.0` |

### If you bind a private port to a VPN or floating address

Binding these private ports to a VPN interface address (Tailscale, WireGuard, a keepalived VIP) rather than loopback is a reasonable way to reach them from your own devices only. It has one sharp edge worth knowing before a power cut finds it for you.

At boot, the container runtime generally starts once the VPN's *service* is active, but active does not mean the address has been **assigned** - the client may still be authenticating. Publishing a port to an address that does not exist yet fails with `cannot assign requested address`, and the container exits immediately. A `restart: unless-stopped` policy does **not** recover this, because the failure is in container networking setup rather than in the container process: the container stays dead until something restarts it by hand. Services bound to `0.0.0.0` come up normally in the same boot, so the stack appears half-working rather than obviously broken.

Two independent mitigations, worth having both:

- `net.ipv4.ip_nonlocal_bind = 1` (via `/etc/sysctl.d/`) lets the bind succeed regardless of when the address appears; traffic flows once it does. This prevents the failure.
- A boot-time script that waits for the address, brings the stack up, and then **verifies the private endpoints actually answer** - recreating any service whose port is not bound. This recovers from it.

Order matters on the way back up: `rtmp-ingest`'s nginx resolves `earshot` once when it loads its config, so it must start (or be restarted) after `earshot` is healthy, or it crash-loops on `host not found in url earshot:1935/live`.

The failure mode that makes this worth automating is not the downtime, it is the silence: **`telemetry` is the alerting path**, so if it is one of the services that failed to bind, nothing can report the outage - including the outage of the alerter itself. Any boot-recovery script should send its own notification, independently of `telemetry`, on success as well as failure.

Internal-only, never published: `earshot`'s RTMP relay + `on_publish` callback (1935 / 80 inside the network), `rtmp-ingest`'s health port (8080 internal), the `srt-gateway` status/health port (8091 internal; discloses the active caller IP, so same loopback-only reasoning as `earshot`'s `/stat`), `earshot`'s direct-DASH listeners (9100 to 9103, one per audio shape and video codec: 4x4 or 1x4, H.264 or H.265; armed by `SRT_DIRECT_LISTENERS`, fed by the owner SRT gateway and, where the operator has set `GUEST_SRT_DIRECT=1`, by the guest one; either way the listener refuses any connection that is not a gateway holding a claimed session), and the `dash-output` / `status-public` volumes.

### Control routes proxied on 8080

`hoast-player` reverse-proxies exactly three `telemetry` routes to the public port (`/api/live`, `/api/start`, `/api/guest/report`), all three as exact-match `location =` blocks, so nothing else on 8090 is reachable from outside:

| Route | Method | Proxies to | Notes |
|---|---|---|---|
| `/api/live` | GET | `telemetry:8090/api/live` | readiness poll while a visitor waits out a cold start |
| `/api/start` | POST | `telemetry:8090/api/start` | starts the loop source; `limit_req` zone `startreq`, 6r/m, burst 3 |
| `/api/guest/report` | POST | `telemetry:8090/api/guest/report` | viewer's abuse report against the live guest session; `limit_req` zone `reportreq`, 1r/m, burst 3, keyed on `$viewer_ip` rather than `$binary_remote_addr` so a tunnel does not share one bucket. nginx passes the reporter's IP and country as `X-Viewer-IP` / `X-Viewer-CC` for the moderation alert |

`/api/stop` is deliberately **not** proxied: stopping the source is the one verb a visitor could use to spoil the demo for everyone else, so it stays on `telemetry`'s own 127.0.0.1-bound port.

The docker socket `telemetry` mounts is read-write, because starting and stopping the source needs it. What keeps that safe is this route list, not the mount: if you add a fourth `/api` route here, it must not pass any request-controlled string into a docker invocation.

### Player URL flags

`?dbg` shows a small on-page diagnostic badge: the build tag, live delay, the video element / drawing buffer / decoded video-frame dimensions, aspect, and `gl.MAX_TEXTURE_SIZE`. It is a read-only overlay for debugging render and sizing issues, and because it needs no dev console it is the way to read that state on a phone. It does not affect playback and is off by default.

**Audio-path flags.** The player can get its audio in one of two ways. The simple one is to let the `<video>` element play it ("element audio"), which is what Firefox and mobile browsers use. The other fetches the audio segments itself and schedules them in Web Audio (the "segment audio feed"), which desktop Chromium needs because it ignores a timing offset in the video track that would otherwise put picture and sound out of step (the empty edit list: [Chromium 537235698](https://issues.chromium.org/issues/537235698), reproducible at [mse-edit-list-repro](https://mormegil6.github.io/mse-edit-list-repro/explained.html)). Each browser gets the right one automatically; these flags override that choice for testing, and neither is needed for normal playback. (The engine-by-engine behaviour, and where each vendor stands on it, is in [docs/UPSTREAM.md](UPSTREAM.md).)

- `?audiofeed` uses the segment audio feed even where it is off by default, i.e. on mobile Chromium, to test decode and sync there before enabling it.
- `?legacyaudio` uses element audio anywhere, to compare the two side by side.

## What `telemetry` itself polls (the monitoring inputs)

- `earshot /stat` → `<publishing/>`, `<nclients>`: is an RTMP publisher live? An owner SRT session on the direct route does NOT appear here (it bypasses nginx-rtmp entirely); `telemetry` ORs in the gateway's own owner latch, so judge that route by segment freshness and the dashboard, not by `/stat`.
- newest `chunk-stream*` segment (`.webm`/`.m4s`/`.mp4`) mtime in the dash volume: segment freshness
- docker container health + the `hoast-player` access log: viewers + countries
- `/sys/class/thermal/thermal_zone0/temp`, `df /`, uptime, load: host health

## What to watch from outside

| Check | Healthy |
|---|---|
| `GET <player-public-url>/` | 200 |
| `GET <player-public-url>/status/status.json` | 200; `live:true` while streaming |
| `GET <player-public-url>/dash/<DASH_NAME>.mpd` | 200 while streaming |
| dashboard (:8090) | reachable **only** on your private/VPN address |
| tunnel / edge (if used) | active |

## Your deployment (fill in; keep OUT of git)

Record the real addresses in a local ops note or in `docker-compose.override.yml`, not in this committed file:

- Host / SSH: `<hostname>` / `<vpn-ip>` (+ any port-forward)
- Player public URL: `https://<your-domain>`
- Telemetry dashboard: `http://<vpn-ip>:8090`
- Ingest: `rtmp://<host-or-public-ip>:1935/owner/<key>`
