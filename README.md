[![Version](https://img.shields.io/github/v/tag/mormegil6/ambisonic-box?label=version&sort=semver&color=2ea44f)](https://github.com/mormegil6/ambisonic-box/releases) [![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](docker-compose.yml) [![FFmpeg](https://img.shields.io/badge/FFmpeg-16--ch%20Opus%20%2B%20VP9%2Fcopy-007808.svg?logo=ffmpeg&logoColor=white)](services/earshot/README.md) [![MPEG-DASH](https://img.shields.io/badge/MPEG--DASH-live-F76935.svg)](docs/ENDPOINTS.md) [![Ambisonics](https://img.shields.io/badge/Ambisonics-3rd%20order%2C%2016ch-8A2BE2.svg)](docs/AMBISONIC-ORDER.md) [![Platform](https://img.shields.io/badge/platform-amd64%20%7C%20arm64-lightgrey.svg?logo=linux&logoColor=white)](.env.example) [![Live demo](https://img.shields.io/badge/live%20demo-stream.bmroz.eu-1F6FEB.svg)](https://stream.bmroz.eu/) [![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

# ambisonic-box: live 360 video and third-order Ambisonics over MPEG-DASH

Containerised toolchain for live streaming 360 video with Higher-Order Ambisonics audio, 1st to 3rd order (3OA, 16ch is the canonical configuration): RTMP in, MPEG-DASH (multichannel Opus, fMP4) out, rendered binaurally in the browser by a [patched HOAST360](https://github.com/mormegil6/hoast360) player that picks the ambisonic order up from the stream.

*Why "Ambisonic Box"? It ships as a Docker container (a box) - which was built and tested on a 2012 Mac Mini (a small box) and a Raspberry Pi 4 (a smaller box). Ambisonic audio, in a box, in a box, in a box. It's boxes all the way down.*

**Live demo:** <https://stream.bmroz.eu/> · **Project page:** <https://bmroz.eu/projects/360-livestream/>

## Quick start

Three commands to a running stack, using the pre-built multi-arch images ([details below](#the-pre-built-images-in-detail)):

```bash
git clone https://github.com/mormegil6/ambisonic-box.git && cd ambisonic-box
./scripts/setup.sh                      # writes .env + generates YOUR OWN publish key
docker compose up -d
# then open http://localhost:8090 - the dashboard links to the other two
```

**On Windows the setup line is `.\setup.cmd`** (or double-click it in Explorer), and everything else is identical. Check [Requirements](#requirements) first: Docker Desktop needs WSL2 and CPU virtualisation enabled in the BIOS.

```
git clone https://github.com/mormegil6/ambisonic-box.git
cd ambisonic-box
.\setup.cmd
docker compose up -d
```

The secrets setup generates never enter any image: they live in `.env` on your host and flow into the containers at start, so the pulled images are the same bytes for everyone. (Building everything yourself instead of pulling is possible too, and slow; that is [for development](#building-from-source-for-development).)

**Three local addresses, and the first one is enough:**

| | Address | What it is |
|---|---|---|
| **Dashboard** | <http://localhost:8090> | service health, stream detail, and links to the other two in its top-right corner |
| Player | <http://localhost:8080> | the 360 viewer, binaural in the browser |
| Earshot monitor | <http://localhost:8081/webtools> | the 16 individual channel faders and live DASH detail |

Every port the stack opens, and which are meant to be public, is in [docs/ENDPOINTS.md](docs/ENDPOINTS.md).

The player is already showing something: on first start the stack synthesises its own demo loop, a spherical test pattern with a 440 Hz source orbiting the listener in 3rd-order Ambisonics, so looking around audibly works with nothing supplied (`DEMO_CONTENT=0` in `.env` turns the synthesis off; the loop then idles until a file appears). To loop your own content instead, put it at `content/demo.mp4` (H.264 + 16-ch AAC) before starting; preparing one from any 16-channel master is [docs/DEMO-CONTENT.md](docs/DEMO-CONTENT.md), and every variable the stack reads is in [`.env.example`](.env.example).

**A playing loop proves the delivery half only.** The `loop-source` service publishes from inside the compose network with a token the stack gave itself, so your encoder, your channel layout, your network path and your credentials are all still untested: see [Stream your own content](#stream-your-own-content).

**Nothing of yours is visible to anyone else.** The player, the operations dashboard and the Earshot monitor all bind to `127.0.0.1`, and the stack makes no outbound connection to publish anywhere. Three INBOUND contribution ports do listen on all interfaces (`1935/tcp`, `8890/udp`, and `8891/udp` once setup has run), each gated by a key, a passphrase or `GUEST_ENABLED`; the port-by-port answer is in [docs/ENDPOINTS.md](docs/ENDPOINTS.md#am-i-broadcasting-to-the-internet-right-now). `docker compose down` stops everything.

If setup or the first `docker compose up` fails, the exact error strings are in [Troubleshooting](#troubleshooting).

### The pre-built images in detail

Every release publishes all the stack's images to ghcr.io for linux/amd64 and linux/arm64, with each architecture's ffmpeg verified GPL-clean before anything is pushed. This is what the quick start runs on, and what spares a first run the expensive part: the `earshot` service compiles ffmpeg from source, which a Raspberry Pi 4 measures in hours.

Setup makes this the default by writing three lines into a fresh `.env`:

```
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.pull.yml
AMBI_BOX_TAG=v1.0.0
```

`COMPOSE_FILE` tells every compose command to use [docker-compose.pull.yml](docker-compose.pull.yml) without `-f` flags, `AMBI_BOX_TAG` pins the release the images come from, and the separator line makes the colon-separated list work on Windows too, which otherwise splits it on semicolons. There is no separate pull step: `docker compose up -d` fetches whatever images it is missing before starting the containers. Upgrading to a newer release is editing that tag and running `docker compose pull && docker compose up -d`. Without setup (say, a hand-made `.env`), the same thing is one `-f` pair per command: `docker compose -f docker-compose.yml -f docker-compose.pull.yml up -d`, which follows `latest` unless `AMBI_BOX_TAG` says otherwise. Verified end to end: the full pipeline check ([`scripts/test-pipeline.sh`](scripts/test-pipeline.sh)) passes on a stack running entirely from the published arm64 images.

### Building from source (for development)

For working on the stack, or tracking `main` between releases - the published images only move when a version is tagged, while a build always matches the tree you are standing on. Pass `--source` to setup and it writes a `.env` with no `COMPOSE_FILE` line, so compose builds everything from this tree (on an existing `.env`, delete that line by hand):

```bash
git submodule update --init             # the patched player, baked in at build
./scripts/setup.sh --source             # .\setup.cmd --source on Windows
docker compose up -d --build
```

The build compiles `earshot`'s nginx-rtmp and ffmpeg fork from source: minutes on a fast amd64 host, hours on a Raspberry Pi. Everything else in the quick start - setup, secrets, the demo loop - is identical on this path.

## Requirements

- Docker Engine with the compose plugin (`docker compose`), buildx for multi-arch builds
- *On Windows:* Docker Desktop needs **WSL2** (`wsl --install`, then reboot) and **CPU virtualisation enabled in the BIOS/UEFI**, on Windows 11 as much as on 10. Docker Desktop's installer will tell you if either is missing, but not before you have downloaded it, and this caught the project's first outside tester
- ~2 GB of image builds on first `compose build` (the `earshot` service compiles its nginx-rtmp and ffmpeg fork from source)
- *Only for the test and measurement scripts:* host `ffmpeg`/`ffprobe`; Node.js (`npm ci`, then `npx playwright install chromium` for the two headless-browser scripts); `python3` + matplotlib for the trade-off plot

## Architecture

<div align="center"> <img src="docs/architecture/architecture.png" width="80%" alt="HOA 360 stream architecture: OBS and loop-source feed rtmp-ingest, earshot transcodes to 16-channel Opus DASH into the dash-output volume, hoast-player serves it to the viewer browser, with shaka packager and telemetry attached to the volume"> </div>

<p align="center"><em>The data path only. Control and monitoring edges are deliberately omitted; <a href="docs/architecture/README.md">docs/architecture/</a> lists exactly what the diagram simplifies.</em></p>

<!-- Diagram source + generator: docs/architecture/ (edit architecture.mmd, run ./build.sh). -->

**What your encoder sends, and what the box does with it.** Whichever route you use, the stream arrives as H.264 video and AAC audio, because that is what OBS can send. From the `earshot` service onward the programme audio is Opus and is never downmixed: the channel count that arrives is the channel count delivered, 16 for 3rd order (the canonical configuration, ACN/SN3D) and 4 for 1st. Beside it the manifest carries one more audio set, a silent stereo keep-alive at 8 kb/s - not a downmix of anything, and not what the player renders. It is there because WebKit suspends a backgrounded video element that has no decodable audio track, and Safari drops the 16-channel Opus set as undecodable.

Both SRT routes take the direct path by default: the tracks pass untouched to the `earshot` service, which combines them and converts to Opus in one operation, so audio is compressed once on the way in rather than twice. What differs is supervision: your **own** stream is authenticated by its passphrase and otherwise left alone, while a **guest** session is arbitrated by `telemetry` throughout - admitted into the single slot, counted, time-limited, and cut off when it must be - whichever transport carries it. `GUEST_SRT_DIRECT=0` reroutes guests through the legacy RTMP hop instead: their tracks are re-encoded to one 16-channel AAC stream and republished into the arbiter over RTMP - the older, blunter enforcement path, at the price of a second lossy compression.

**Why 16 channels and not 25**, and the two candidate routes past that ceiling, are in [docs/AMBISONIC-ORDER.md](docs/AMBISONIC-ORDER.md). The short version: ffmpeg's AAC encoder writes the required Program Config Element only for the layouts in its own table, and on the FFmpeg 7.1 this stack pins that means 4 and 16 pass while 9 and 25 are refused. Master [has since gained](https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/24218) a 9-channel ambisonic entry, which no release carries yet and which never covered 25. That binds the AAC contribution leg only - the on-demand path never touches AAC and is 4th-order verified end to end.

**Video codec.** `docker-compose.yml`'s `FFMPEG_FLAGS` fallback is the single source of truth, currently **H.264 passthrough** (`-c:v copy`). VP9 is the codec *policy* and ships as a ready-to-uncomment line in `.env.example`, not the running default: at 4K it costs roughly fifteen times the CPU of passthrough and peaks at 100 C on the reference host. Check yours rather than trusting this paragraph:

```bash
docker compose config | grep FFMPEG_FLAGS
```

`dash-output` in the diagram is a Docker volume: the shared filesystem `earshot` segments into and nginx serves from.

| Service | Role | Host port |
|---|---|---|
| [`srt-gateway`](services/srt-gateway/) | SRT contribution ingest for GUESTS (native OBS multitrack); joins 4x4 to 16-ch, republishes into the guest arbiter. Privilege-separated. The operator's own route runs a second instance, `srt-gateway-owner` on 8891/udp, written by `scripts/setup.sh`, which feeds `earshot` directly instead. See `SRT_ENABLED` / `GUEST_ENABLED` / `SRT_DIRECT` | 8890/udp |
| [`rtmp-ingest`](services/rtmp-ingest/) | RTMP contribution ingest (legacy route); stream-key auth, relay to `earshot` | 1935 |
| [`earshot`](services/earshot/) | transcode to 16-ch Opus + video per `FFMPEG_FLAGS` (H.264 passthrough default, VP9 opt-in), live DASH segmenting ([Envelop Earshot](services/earshot/README.md), submodule of a fork) | 8081 (dev monitor) |
| [`loop-source`](services/loop-source/) | demo contribution encoder: loops `content/demo.mp4` | - |
| [`hoast-player`](services/hoast-player/) | viewer origin: patched HOAST360 player + `/dash/` | 8080 |
| [`telemetry`](telemetry/) | ops dashboard + breakage-only alerts + curated public status.json ([telemetry/](telemetry/README.md)) | 8090 (bind private) |
| `shaka` | Shaka Packager, offline only, never in the live path. Main job: [`scripts/package-vod-dash.sh`](scripts/package-vod-dash.sh) runs the image standalone to package the on-demand clips into `content/vod/dash/` (the compose `tools` profile additionally drives the optional A/V-sync variant packaging, [`scripts/package-dash-variants.sh`](scripts/package-dash-variants.sh)) | - |

### What it looks like running

Two operator-facing views of the same running stream:

<div align="center"> <img src="docs/images/telemetry-dashboard.png" width="85%" alt="The telemetry dashboard: a services row showing srt-gateway, rtmp-ingest, earshot, hoast-player and telemetry all healthy; reachability chips for the tunnel, the VOD origin and the backup; stream detail (resolution, bitrate, egress, RTMP links, segment age); host load, memory, disk and uptime; and three-hour history sparklines for viewers, CPU temperature and stream liveness."> </div>

<p align="center"><em>The custom telemetry dashboard on <code>:8090</code>: service health, reachability, stream detail, host load and three-hour history. Details in <a href="telemetry/README.md">telemetry/README.md</a>.</em></p>

<div align="center"> <img src="docs/images/earshot-webtools.png" width="85%" alt="Earshot's own built-in debug monitor at :8081/webtools, showing the live 360 video preview of the demo concert recording, server and video representation info (4096x2048, avc1 H.264 passthrough), the DASH stream info panel, and an individual gain slider for each of the 16 ambisonic audio channels, all at zero"> </div>

<p align="center"><em>Earshot's own built-in monitor on <code>:8081</code>, which is where the 16 individual audio channels and the live DASH representation details are actually visible.</em></p>

<div align="center"> <img src="docs/images/quest3-browser-capability.jpg" width="85%" alt="The VOD page open in a Meta Quest 3 browser at stream.bmroz.eu/vod/?dbg, showing the 360 test card rendered with the ambisonic energy overlay, and a diagnostic panel reporting that 2-, 16- and 25-channel Opus all decoded"> </div>

<p align="center"><em>A Meta Quest 3 browser playing the stream at stream.bmroz.eu, with the <code>?dbg</code> capability probe reporting that 2-, 16- and 25-channel Opus all decoded on the headset itself. What that probe implies about ambisonic order is in <a href="docs/AMBISONIC-ORDER.md">docs/AMBISONIC-ORDER.md</a>.</em></p>

<div align="center">
<table>
  <tr>
    <td valign="center"><img src="docs/images/iphone-xs-safari-vod.jpeg" height="380" alt="The Directions on-demand clip playing on an iPhone Xs in Safari, showing the 360 test card and the on-screen indicator confirming reads are panned in 3rd-order Ambisonics, 16 channels"></td>
    <td valign="center"><img src="docs/images/macos-safari27-live.jpeg" height="290" alt="The live stream playing in macOS Safari 27, with the About Safari dialog open over it confirming the version"></td>
  </tr>
</table>
</div>

<p align="center"><em>Neither Safari decodes this stream's 16-channel Opus natively, on macOS or iOS. It plays anyway, decoded in WebAssembly. The full story, including the two live-playback failures, how both were traced to their mechanisms, and what was reported upstream, is in <a href="docs/IOS-SAFARI.md">docs/IOS-SAFARI.md</a>.</em></p>

## Stream your own content

**What a working demo loop does and does not prove.** It exercises the whole delivery half - transcode, 16-channel Opus, DASH segmenting, the player, the binaural render - so if it plays, that half is sound. It exercises **none of the contribution half**, because `loop-source` publishes from inside the compose network with a token the stack gave itself. Your encoder, your channel layout, your network path and your credentials are all still untested at that point, and that is exactly where first-time setups actually fail. Treat a playing loop as "the box works", not as "my stream will work".

Two ways in. **SRT is the recommended one**: stock OBS, no patched fork, the same recipe on macOS and Windows. RTMP is the legacy route and needs [OBS Studio Music Edition](https://github.com/pkviet/obs-studio/releases/), a Windows-only fork, because RTMP carries only one audio track.

| | SRT (recommended) | RTMP (legacy) |
|---|---|---|
| Sender | stock OBS, macOS or Windows | OBS Music Edition, Windows only |
| Audio | one 4-channel track (1st order) or four (3rd order), detected from the stream | one AAC track, 4 or 16 channels, passed straight through (a single track, so there is nothing to detect) |
| Endpoint | `srt://<box>:8891?streamid=owner&passphrase=…` | `rtmp://<box>:1935/owner?token=<RTMP_OWNER_KEY>`, any stream key |

Run [`./scripts/setup.sh`](scripts/setup.sh) first ([`.\setup.cmd`](setup.cmd) on Windows). It generates your own key and passphrase and **prints the full SRT URL with the passphrase already filled in** - paste that rather than retyping it. Lost it? `docker compose exec srt-gateway-owner printenv SRT_OWNER_PASSPHRASE`.

The core OBS settings, identical on both platforms:

| Setting | Value |
|---|---|
| Settings > Audio > Channels | **`4.0`** |
| Output Mode | **Advanced**, then the **Recording** tab |
| Type / Output Type | **Custom Output (FFmpeg)** / **Output to URL** |
| Container Format | **mpegts** |
| Audio Encoder | plain **`aac`** (tick "Show all codecs" if hidden) |
| Audio Track | tick **1, 2, 3, 4** |
| Start it with | **Start Recording**, not Start Streaming |

Custom Output (FFmpeg) lives on the *Recording* tab even though it streams to a URL, so Start Recording is the button that pushes.

**The per-OS guides show the exact OBS steps, screenshots included, together with the one genuinely OS-specific part - routing four 4-channel sources into those tracks:**

- **[docs/obs-macos.md](docs/obs-macos.md)** - a multichannel Core Audio device ([BlackHole](https://github.com/ExistentialAudio/BlackHole))
- **[docs/obs-windows.md](docs/obs-windows.md)** - ASIO, via [REAPER](https://www.reaper.fm/)'s ReaRoute and the [atkAudio plugin](https://obsproject.com/forum/resources/atkaudio-plugin.2099/)

The guides are meant to be followed literally, and they also explain why: `4.0` and never `7.1`, plain `aac` and never `mp2` or `aac_at`, `latency` in microseconds, `pkt_size=1128` through a tunnel, and the leading space that silently turns the URL into a filename. Each was established by pushing a per-channel tone ladder through real hardware, because each obvious-looking alternative fails **without any error**.

Channel counts: 4 and 16 pass through as they are, a 2nd-order source must be zero-padded to 16 by the sender, and stereo or mono produces no output at all. Why 16 is the ceiling today, and what would lift it, is in [docs/AMBISONIC-ORDER.md](docs/AMBISONIC-ORDER.md). Bitrate guidance is in [docs/BITRATE.md](docs/BITRATE.md), together with this project's own measurement of the AAC ladder ([aac-bitrate-test/RESULTS.md](aac-bitrate-test/RESULTS.md): [AMBIQUAL](https://github.com/QxLabIreland/Ambiqual), with [BAM-Q](https://doi.org/10.1109/TASLP.2019.2904850) as a second opinion).

The stream appears at `http://<host>:8080/dash/<DASH_NAME>.mpd` (default `hoast_demo`), which is what the bundled player requests. `DASH_NAME` is independent of your keys, so rotating a credential never moves the manifest URL.

## Guest test endpoint (the `guest` application)

Anyone with an ambisonic microphone rig and OBS can test their stream against this stack without standing up their own server: a keyless application that borrows the whole pipeline for the duration of a session.

> **Try it on the public demo, no key needed:** point OBS (settings as in [Stream your own content](#stream-your-own-content)) at:
>
> `srt://ingest.bmroz.eu:8890?streamid=<any-name>&latency=2000000&pkt_size=1128`
>
> Your stream is then visible at: <https://stream.bmroz.eu/>
>
> One guest at a time, three hours per session; the demo loop yields while you are live and returns when you stop. The university's port forward is provisional, so the public endpoint can be temporarily gone. Behind a NAT or proxy that rewrites source addresses, read the address-keyed-rules note in [docs/GUEST-ENDPOINT.md](docs/GUEST-ENDPOINT.md) first.

Sessions are arbitrated by `telemetry`, which holds the single slot, the session cap, the reconnect grace, the cooldown, the ban list and the dashboard's End session button. The full rule set, the resource guard, what a guest sees when refused, and how to run the endpoint responsibly are in **[docs/GUEST-ENDPOINT.md](docs/GUEST-ENDPOINT.md)**.

**Disabled by default.** Most deployments are a single private publisher and should never expose a keyless application; set `GUEST_ENABLED=1` to opt in. Off, the `guest` application does not exist in the ingest config and the status pages carry no trace of it.

## On-demand VOD clips

**Off by default:** the stack's purpose is live streaming, VOD is opt-in. Set `VOD_ENABLED=1` to serve the on-demand page at `/vod/`; disabled, the `/vod/` and `/vod-dash/` routes return 404.

Two reference clips are published: [`directions`](https://stream.bmroz.eu/vod/?clip=directions) (a 360 orientation test, spoken direction reads panned in third-order Ambisonics under an energy-visualisation overlay that shows where each read is supposed to come from, so a listener can hear whether the delivered audio still agrees with the picture) and [`colortones`](https://stream.bmroz.eu/vod/?clip=colortones) (a colour-and-tone A/V-sync pattern). No media is committed here - the masters, the 8K test card and the caption sidecars ship as [release assets](https://github.com/mormegil6/ambisonic-box/releases/tag/vod-clips), and only the generators and the player wiring are tracked. With `VOD_ENABLED=1` the two masters are fetched once on first start (~185 MB, in the background, SHA-256 verified against pinned hashes, fail-soft). Those masters are the **input to packaging, not the served clips**: run [`scripts/encode-vod-ladder.sh`](scripts/encode-vod-ladder.sh) then [`scripts/package-vod-dash.sh`](scripts/package-vod-dash.sh) to produce `content/vod/dash/`, or point `vodBase` in `brand.json` at a host already serving that same packaged `vod/dash/` tree, typically an object-storage bucket under a `vod-dash/` prefix ([docs/VOD.md](docs/VOD.md) has the bucket recipe, CORS included). Without one of the two, `/vod/` loads and every clip fetch 404s against an empty directory.

Generation, packaging, the 360 test card and its projection check, captions, headset playback and serving VOD from object storage: [docs/VOD.md](docs/VOD.md).

<div align="center"> <img src="docs/images/directions-energy-frame.png" width="70%" alt="A frame of the directions reference clip: the equirectangular 360 test card with a bright energy-visualisation glow sitting over the wall labelled RIGHT, at the moment the word right is spoken"> </div>

<p align="center"><em>One frame of the <a href="https://stream.bmroz.eu/vod/?clip=directions"><code>directions</code></a> reference clip. The glow was rendered from the ambisonic stem before encoding and is baked into the picture, so it cannot move: together with the wall label it is a fixed reference for where the sound is supposed to be. What is under test is the audio. Here the word being spoken is <em>right</em>, so it should be heard from the right; if it arrives from anywhere else, the delivery chain has scrambled the channels while the picture stayed put.</em></p>

## Configuration

[`scripts/setup.sh`](scripts/setup.sh) creates `.env` from [`.env.example`](.env.example), which documents each variable at the point it is set; the per-service ones are in [docker-compose.override.yml.example](docker-compose.override.yml.example). The handful worth knowing before you edit anything:

| Variable | Default | Purpose |
|---|---|---|
| `RTMP_OWNER_KEY` | none; **`scripts/setup.sh` generates one** | The security-relevant publish secret at rtmp-ingest. The stack refuses to start on the placeholder unless `ALLOW_DEFAULT_OWNER_KEY=1`. |
| `FFMPEG_FLAGS` | the `docker-compose.yml` fallback | Video policy of the `earshot` transcode; the programme audio is always 16-ch Opus, and the `-dash_segment_type` in this same value also picks the codec of the silent stereo keep-alive track: AAC when it is `mp4`, which is the default, and Opus when it is `webm`, which is the commented-out VP9 line in [`.env.example`](.env.example). Check the effective value with `docker compose config \| grep FFMPEG_FLAGS`. |
| `DASH_NAME` | `hoast_demo` | Public manifest filename, served at `/dash/<DASH_NAME>.mpd`. |
| `SRT_ENABLED` | `1` | SRT contribution ingest, the recommended route. It still admits nobody unless a passphrase matches. |
| `GUEST_ENABLED` | `0` | The keyless guest test endpoint. Off means the `guest` application does not exist. |
| `VOD_ENABLED` | `0` | On-demand clips. Off by default: the stack's purpose is live. |

**Setting up without [`scripts/setup.sh`](scripts/setup.sh).** Copy [`.env.example`](.env.example) to `.env` and set `RTMP_OWNER_KEY` and `LOOP_SOURCE_KEY` to any random strings of about 30 letters and digits. A hand-made `.env` has no `COMPOSE_FILE` line, so plain `docker compose up -d` builds from source - run `git submodule update --init` first (the player bakes into its image), or add the three lines from [The pre-built images in detail](#the-pre-built-images-in-detail) to pull instead. This skips the owner SRT route on UDP 8891; to get that too, copy [docker-compose.override.yml.example](docker-compose.override.yml.example) and set `SRT_OWNER_PASSPHRASE`. Running setup is still the easier path, because it prints the SRT URL with your passphrase already in it.

Two things are deliberately *not* env-tunable: the audio policy (16-ch Opus, hardcoded upstream in Earshot) and the live-edge distance. The `earshot` image build patches ffmpeg's DASH muxer to floor `suggestedPresentationDelay` at 30 s (`DASH_SPD_FLOOR` build arg), so players join ~30 s behind the live edge by design. That is the price of gap-free playback of a 16-channel live stream.

## Test and measurement scripts

| Script | Purpose |
|---|---|
| [`scripts/test-pipeline.sh`](scripts/test-pipeline.sh) | synthetic end-to-end test: 16 sine channels + test video pushed through ingest auth, asserts live 16-ch Opus DASH appears with the video codec the effective `FFMPEG_FLAGS` implies; also guards that README, `.env.example` and the compose fallback agree. PASS/FAIL |
| [`scripts/make-lipsync-scene.sh`](scripts/make-lipsync-scene.sh) | cut a GOP-matched, tv-range transient excerpt for by-ear lip-sync judging |
| [`scripts/package-dash-variants.sh`](scripts/package-dash-variants.sh) | package a WebM master into 0.5/1/2/4 s DASH variants for the comparison page (`lip-sync-test/index.html`) |
| [`scripts/measure-lipsync.js`](scripts/measure-lipsync.js) | headless-Chromium A/V measurement over the packaged variants ([results](lip-sync-test/RESULTS.md)) |
| [`scripts/plot-segment-tradeoff.py`](scripts/plot-segment-tradeoff.py) | regenerate the segment-duration trade-off figure |
| [`scripts/plot-opus-compression.py`](scripts/plot-opus-compression.py) | regenerate the compression-level quality figure |
| [`scripts/measure-opus-compression.sh`](scripts/measure-opus-compression.sh) | libopus `-compression_level` A/B on real ambisonic material, scored with AMBIQUAL ([results](opus-compression-test/RESULTS.md)) |
| [`scripts/measure-aac-bitrate.sh`](scripts/measure-aac-bitrate.sh) | contribution AAC bitrate ladder on real ambisonic material, scored with AMBIQUAL ([results](aac-bitrate-test/RESULTS.md)) |
| [`scripts/plot-aac-bitrate.py`](scripts/plot-aac-bitrate.py) | regenerate the AAC bitrate-vs-quality figure |
| [`scripts/pick-excerpt.py`](scripts/pick-excerpt.py) | choose a measurement excerpt by content - level, spectral flatness, steadiness - rather than by a fixed offset |
| [`scripts/smoke-hoast360.js`](scripts/smoke-hoast360.js) | headless-browser smoke test of the patched player |

`package-dash-variants.sh` drives Shaka Packager through the compose `tools` profile. The pattern for manual runs is `docker compose run --rm shaka <packager args>`.

**Shaka is built here, not pulled**, so the first packaging run on a fresh clone needs it once:

```
docker compose --profile tools build shaka
```

The official image writes the Opus `dOps` box little-endian, so Chromium's MSE refuses the audio it produces, and with it every DASH player. That is [shaka-packager#1627](https://github.com/shaka-project/shaka-packager/issues/1627), unfixed in every release so far, and it bites when packaging Opus from WebM into MP4; packaging an existing Opus MP4 is unaffected, because the reader carries the mirror defect and the two cancel out. [`services/shaka`](services/shaka/) holds the patch and the recipe, and the packaging scripts say so if the image is missing. When a release carries the fix, restore the official image in the compose service and delete that directory.

## Troubleshooting

### First run

| Symptom | Cause | Fix |
|---|---|---|
| `docker compose up` refuses with `required variable LOOP_SOURCE_KEY is missing a value`, or the same message naming `RTMP_OWNER_KEY` | Setup has not run: there is no `.env`, or it has no value for that key | Run `./scripts/setup.sh` (`.\setup.cmd` on Windows) and try again. The check happens before anything is pulled, built or created, so there is nothing to clean up first |
| `dependency failed to start: container ambi-box-rtmp-ingest-1 is unhealthy` | An `.env` that exists but still carries a placeholder key committed to this repository. `rtmp-ingest` refuses to serve it, because port 1935 is published on all interfaces and both values are public. Compose swallows the reason; `docker compose logs rtmp-ingest` prints it | Re-run setup: it repairs an existing `.env` in place, without touching a key you chose yourself |
| On Windows, `bash scripts/setup.sh` hangs, opens an unfamiliar Linux shell, or reports no WSL distro | Git for Windows puts `cmd\` on PATH and not `bin\`, so `bash` there is the WSL launcher in `System32` rather than Git Bash | Run `.\setup.cmd` instead. It is a launcher for the same `scripts/setup.sh`: it looks for Git Bash in the usual places, then in the registry, then follows `git` on your PATH, and failing all of those runs the script in a container |
| On Windows, setup fails with `set: -: invalid option` or `bash\r: No such file or directory` | The clone predates the `.gitattributes` that pins line endings, so the shell scripts are checked out with CRLF | `git add --renormalize .` then `git checkout -- .`, as two separate commands (`&&` is not valid in Windows PowerShell), or clone again |

### Running

| Symptom | Cause | Fix |
|---|---|---|
| Relay `Connection refused` after recreating `earshot` | `rtmp-ingest` resolves `earshot`'s address once, at startup | `docker compose restart rtmp-ingest` |
| Segments longer than `-seg_duration` (e.g. 3.34 s) | GOP duration does not divide the segment target, so no keyframe lands on the boundary and the muxer closes at the next one (`-g 50` at 29.97 fps is a 1.668 s GOP, so the first keyframe at or after 2 s falls at 3.336 s) | set `-g` so the segment duration is an integer multiple of the GOP (`-g 60` @ 29.97/30, `-g 50` @ 25); equality is the preferred default for the live encode |
| Browser loops `PIPELINE_ERROR_DECODE` | full-range (pc) VP9 source breaks the dash.js/MSE path | re-encode to limited range: `-vf scale=in_range=pc:out_range=tv -color_range tv` |
| `loop-source` idle although `demo.mp4` exists | file presence is checked once at startup | `docker compose restart loop-source` |
| Publisher dies seconds into a 4K push | RTMP message limit smaller than 4K keyframes | keep `max_message 10M` in the nginx-rtmp configs (already set here) |

## Working directories: `output/` and `scratch/`

Two gitignored directories at the repo root, with opposite guarantees.

`output/` is **`earshot`'s** working directory: the DASH volume is bind-backed by it, and `earshot`'s entrypoint clears that directory on every container start so each run begins a fresh timeline. Anything you leave in `output/` is deleted by the next `docker compose up`. That is intended for segments, and a trap for everything else.

`scratch/` is mounted alongside at `/opt/data/scratch` and is never touched by `earshot`, which only ever clears `.../dash`. Put anything that has to survive a restart here: test harnesses, probe captures written from inside the container, one-off files. Because it sits inside the repo, Node also resolves the root `node_modules` from it, so browser-based harnesses run without extra path setup.

## Deployment

| Host | Status |
|---|---|
| Local / lab (AMD64) | the [quick start](#quick-start) above; validated on WSL2 Ubuntu 24.04 LTS and on Ubuntu Server 26.04 LTS, the reference deployment host |
| Raspberry Pi 4 (ARM64) | **validated end to end** on Raspberry Pi OS 13 (trixie), 64-bit, over the RTMP/loop path: real 16-channel Opus DASH from a real publish, and 20 minutes of sustained transcoding at 32-34 % CPU and 54.5-65.7 C with no throttling. Re-measured 2026-08-10 on Debian 13: a cold native build is 19m38s (`earshot` alone 14m56s), a four-core compile peaks at 71.0 C and `throttled=0x0` across all 198 samples with the clock held at 1500 MHz. That run predates the fan being reconfigured: it was taken under the `gpio-fan` overlay, which drives the official case fan fully on or fully off against a single 70 C trip, and the host has since moved to `pwm-gpio-fan` with four speed steps from 68 C to 78 C. The numbers describe the bang-bang configuration and are due a re-run. Guest handover there is 0.5 s median, 1.0 s worst, against a 4 s budget. **Not measured on arm64:** sustained 4K transcode load as opposed to compile load, and concurrent-viewer capacity |
| Azure | planned, not yet validated; the constraint is raw L4 ingress for the contribution leg (UDP 8890/8891 for SRT, TCP 1935 for RTMP), which HTTP-only front ends cannot carry |

Per-host settings (a private bind for the dashboard, host metric mounts, Telegram tokens, branding) go in `docker-compose.override.yml`, which is gitignored and always loaded - automatically when no `COMPOSE_FILE` is set, and as part of the list setup writes otherwise. [`scripts/setup.sh`](scripts/setup.sh) writes one containing the owner SRT route; [docker-compose.override.yml.example](docker-compose.override.yml.example) documents the rest, block by block, to copy from as needed. The base stack runs without any of it.

Measurements, the two arm64 build traps this repo already fixes, and what belongs in an override: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Documentation

- [docs/AMBISONIC-ORDER.md](docs/AMBISONIC-ORDER.md): why the live path stops at 16 channels, what is already 4th-order verified, and the two routes past the AAC ceiling
- [docs/BITRATE.md](docs/BITRATE.md): contribution bitrate, audio and video, with the published anchors
- [docs/GUEST-ENDPOINT.md](docs/GUEST-ENDPOINT.md): the guest session rules in full, and the `SRT_MODE=owner` route
- [docs/VOD.md](docs/VOD.md): on-demand clips, the 360 test card, captions, headset playback, object-storage delivery
- [docs/obs-macos.md](docs/obs-macos.md) / [docs/obs-windows.md](docs/obs-windows.md): the per-OS sender recipes, step by step
- [docs/ENDPOINTS.md](docs/ENDPOINTS.md): every port/endpoint the stack exposes, public vs private, what to monitor, and the port-by-port answer to whether the box is broadcasting anything right now
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): where the stack has run, what was measured on each host, and what belongs in a per-host override
- [telemetry/README.md](telemetry/README.md): monitoring service (dashboard + alerts + public status.json)
- [docs/CI.md](docs/CI.md): the four CI workflows, why each check exists, and what they deliberately do not cover
- [services/earshot/README.md](services/earshot/README.md): Earshot vendoring provenance and local patches
- [docs/UPSTREAM.md](docs/UPSTREAM.md): bugs this stack found and sent back to the projects they belong to, merged and open, plus the ones still prepared and unsent
- [docs/CHROME-MULTICHANNEL-OPUS.md](docs/CHROME-MULTICHANNEL-OPUS.md): a Chrome experiment that breaks every Opus decode above 2 channels, why the usual isolation steps do not find it, and the one-flag workaround
- [docs/IOS-SAFARI.md](docs/IOS-SAFARI.md): no Safari decode surface accepts this stream's 16-channel Opus, so the player decodes it in WebAssembly instead; what that takes on iOS, and the on-demand clips playing on an iPhone
- [tests/av-sync/README.md](tests/av-sync/README.md): the browser-console instruments built during the A/V-desync investigation, and how to run them against the colour+tone clip
- [docs/fixtures/README.md](docs/fixtures/README.md): the two fixtures that reproduce the exact setups the OBS guides were verified with
- [docs/architecture/README.md](docs/architecture/README.md): the source for the data-flow diagram at the top of this README, and how to regenerate it
- [.env.example](.env.example): configuration reference, every variable the stack reads
- [docs/DEMO-CONTENT.md](docs/DEMO-CONTENT.md): the demo loop, and preparing `content/demo.mp4` from any 16-channel master

## Measurements

Some measured results behind this README (transcode thermals, the bitrate and temperature ladder, codec constraints, AV1 viability) are being written up for publication and are not in the repo; this README will carry the citation and the tagged commit once the papers are out. Studies that are finished and justify a decision the stack actually made do ship here, with their raw data and regeneration scripts:

- [lip-sync-test/RESULTS.md](lip-sync-test/RESULTS.md): the segment-duration study - measured across 0.5/1/2/4 s variants. Segment duration turns out **not** to affect A/V sync (a structural 0 ms offset at every duration); it is a bitrate and buffer-depth trade-off, which is why 2 s is the default
- [opus-compression-test/RESULTS.md](opus-compression-test/RESULTS.md): what libopus `-compression_level` is worth on 16-channel ambisonics, judged with AMBIQUAL on real recordings. Answer: leave it unset - there is no meaningful CPU to reclaim (0.9 % of a core), and solo piano is the one material that shows any degradation
- [aac-bitrate-test/RESULTS.md](aac-bitrate-test/RESULTS.md): where the contribution leg's 96 kbit/s/channel setting actually sits, measured against AMBIQUAL and, as a second opinion in a different domain, BAM-Q. The AAC-then-Opus cascade a viewer receives flattens 1.6-1.96x slower than the AAC leg alone from 64 to 128, and the gap is already open by 96. Building the binaural render for BAM-Q also surfaced and fixed a real defect in HOAST360's decoding-filter loading, upstream since 2020, that cost more than any codec setting tested

## License

Compose files, service configs and scripts in this repository: **Apache 2.0**. The published media is licensed separately, and everything bundled or built keeps its own license:

| Component | License |
|---|---|
| **Reference clips and media** shipped as [release assets](https://github.com/mormegil6/ambisonic-box/releases/tag/vod-clips): the `directions` and `colortones` clips, the 8K 360 test card, and the caption sidecars | **[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)** - reuse, remix and redistribute freely, including commercially, with attribution (one disclosed exception in [docs/VOD.md](docs/VOD.md#licence)) |
| [HOAST360](https://github.com/mormegil6/hoast360) (patched fork, git submodule) | GPL-3.0-or-later |
| [Envelop Earshot](https://github.com/EnvelopSound/Earshot) (submodule at `services/earshot/src`, tracking [a fork](https://github.com/mormegil6/Earshot)) | GPL |
| Envelop/pkviet FFmpeg fork (built inside the `earshot` image) | GPL v3 (built with `--enable-gpl`; the default build (`ENABLE_NONFREE=0`) is redistributable, and only an explicit `ENABLE_NONFREE=1` build carries the non-redistributable `--enable-nonfree` stamp (services/earshot/README.md section 7)) |
| [nginx-rtmp-module](https://github.com/arut/nginx-rtmp-module) | BSD-2-Clause |
| [Shaka Packager](https://github.com/shaka-project/shaka-packager) (official image) | BSD-3-Clause |
| nginx, Alpine packages | BSD-2-Clause / various |

## Citation

This repository is the containerised successor of the toolchain described in the paper below. If you use this work in research, please cite it:

```bibtex
@inproceedings{mroz2023toolchain,
  title     = {A Commonly-Accessible Toolchain for Live Streaming Music Events
               with Higher-Order Ambisonic Audio and 4K 360 Vision},
  author    = {Mr{\'o}z, Bart{\l}omiej and Odya, Piotr and Danowski,
               Przemys{\l}aw and Kabaci{\'n}ski, Marek},
  booktitle = {Proceedings of the AES International Conference on Spatial and
               Immersive Audio},
  publisher = {Audio Engineering Society},
  month     = aug,
  year      = {2023},
  url       = {https://www.aes.org/e-lib/browse.cfm?elib=22204}
}
```

## Acknowledgments

- Thomas Deppisch and Nils Meyer-Kahlen, [HOAST360](https://github.com/thomasdeppisch/hoast360), the higher-order Ambisonics 360 player this project patches and serves
- [Envelop](https://envelop.us), Earshot, the multichannel RTMP-to-DASH transcoder
- The [OBS Project](https://obsproject.com/), OBS Studio - the recommended sender needs no fork at all
- pkviet, [OBS Studio Music Edition](https://github.com/pkviet/obs-studio) and the PCE-capable FFmpeg fork, which carried the 16-channel RTMP path before that
- Existential Audio, [BlackHole](https://github.com/ExistentialAudio/BlackHole), the macOS multichannel loopback driver the macOS recipe routes through
- atkAudio, [its OBS plugin](https://obsproject.com/forum/resources/atkaudio-plugin.2099/), which is what gets ASIO into OBS on Windows
- [Shaka project](https://github.com/shaka-project), Shaka Packager
- [AmbisonicEnergyRenderer](https://github.com/mormegil6/AmbisonicEnergyRenderer), which renders the directional-energy overlay in the `directions` clip from the raw ACN/SN3D signal (spherical-harmonic decode onto a t-design, k-NN inverse-distance gridding, streamed straight into ffmpeg)
- Gdańsk University of Technology, [Department of Multimedia Systems](https://multimed.org/index_en.html), which hosts this deployment and provides its public network access

## Contact

Bartłomiej Mróz · bartlomiej.mroz@pg.edu.pl · Department of Multimedia Systems, Gdańsk University of Technology · [bmroz.eu](https://bmroz.eu)
