# `earshot` submodule - a fork of Envelop Earshot

`src/` is a git submodule pointing at [mormegil6/Earshot](https://github.com/mormegil6/Earshot), a fork of [EnvelopSound/Earshot](https://github.com/EnvelopSound/Earshot), GPL licensed (see `src/LICENSE`).

It was a hand-vendored copy until 2026-08-18. Vendoring existed only because there was no push access to Earshot until 2026-08-08; once maintainer status was live, the reason for vendoring was gone and the fork+submodule pattern already proven by `hoast360` (see the top-level `.gitmodules`) applied equally here: fix in the fork, bump the pointer, no `branch =` pin. One concrete cost of the vendored years is now gone too - the ~180 Dependabot alerts attached to `webtools/yarn.lock` (a 2020 npm tree on a `node:12` base) no longer show up in this repo's own Security tab, since submodule content isn't part of this repo's dependency graph. They didn't vanish; they moved to the fork, which is where they're actually actionable.

## Relationship to upstream

The fork tracks `EnvelopSound/Earshot`'s `master` directly (real GitHub fork relationship, `git fetch origin && git merge --ff-only origin/master` when catching up, same as any fork). General-purpose fixes are contributed upstream as PRs from the fork's own branches; the full history of what was sent where and its current status (merged / open / not yet upstreamable) is tracked centrally in [docs/UPSTREAM.md](../../docs/UPSTREAM.md), not duplicated here.

## What's still genuinely local to the fork

Everything that has no upstream analogue lives directly in the fork's own commit history, with the rationale in the code itself (each patch carries a comment explaining the WHY, often with real measurements) rather than repeated here. As of the fork's `73e0ecc`:

| file | what, briefly |
|---|---|
| `Dockerfile` | `socat` and the `direct-dash-gate.sh` `COPY`, both for the SRT direct-DASH listeners |
| `nginx-transcoder/nginx.conf` / `nginx-no-ssl.conf` | `-b:a:0 1024k` on the Opus encode, plus the silent stereo keep-alive output that gives the live manifest its third AdaptationSet: the `anullsrc` input, the explicit `-map`s, the per-stream `:a:0`/`:a:1` options, `-shortest` and `-c:a:1 ${KEEPALIVE_CODEC} -b:a:1 8k -ac:a:1 2`. The `dashName` field on `/nginxInfo` was also local until it went upstream in [#84](https://github.com/EnvelopSound/Earshot/pull/84) |
| `nginx-transcoder/entrypoint.sh` | the SRT direct-DASH listener block (~150 lines: `socat` gate on `:9100` to `:9103`, `JOIN_MAP` derivation, the log-permission workaround) - this deployment's owner-SRT route bypasses RTMP entirely, which stock Earshot has no model for at all - and the `KEEPALIVE_CODEC` derivation from `FFMPEG_FLAGS`, exported once so the three exec sites cannot disagree about the keep-alive track's codec |
| `nginx-transcoder/direct-dash-gate.sh` | new file: the peer-IP admission gate for the two direct-DASH listeners |
| `webtools/src/Webtools.js` | `probeDirectStream()` and the `dashName`/`directStream` state pair - the client half of the same direct-DASH detection |
| `.dockerignore` | new file: an explicit allowlist, because a submodule checkout is the whole repo tree rather than the curated subset a vendored copy had, so the build context has to be narrowed deliberately |
| `webtools/yarn.lock` | `moment` 2.29.4 to 2.30.1, not yet offered upstream |

The SRT direct-DASH work is not upstreamable on its own terms: it is architecture specific to this deployment, not a generic Earshot fix. The rest of the table is a different matter and the status is in [docs/UPSTREAM.md](../../docs/UPSTREAM.md) - `nginx.conf`'s `wait_key`/`wait_video` fix merged upstream as [#53](https://github.com/EnvelopSound/Earshot/pull/53), and the `GainSliderBox.js` row is a comment left behind by a fix that is already upstream via [#64](https://github.com/EnvelopSound/Earshot/pull/64).

## What this service does in the stack

`earshot` takes contribution two ways, and which one carries your stream depends on how it arrived.

**The direct listeners, which the default routes use.** `SRT_DIRECT_LISTENERS` (default 1, `docker-compose.yml`) arms four compose-internal sockets. The port carries both the audio shape and the video codec, because the MP4 codec tag cannot be deferred to ffmpeg: `:9100` and `:9102` join four 4-channel tracks to 16 channels, `:9101` and `:9103` pass a single quad through, with the first of each pair tagging H.264 (`avc1`) and the second H.265 (`hvc1`). Both SRT gateways hand raw MPEG-TS to them - the owner route since 2026-08-09, guests since 2026-08-21 - so nothing on that path touches RTMP or the AAC re-encode. `direct-dash-gate.sh` admits only a gateway holding a claimed session.

**The RTMP ingress**, which serves the legacy contribution route (OBS Music Edition, the demo loop, and guests with `GUEST_SRT_DIRECT=0`): nginx-rtmp accepts the relayed stream from `rtmp-ingest` on :1935 (internal only) and `exec`s the PCE-aware ffmpeg fork per published stream:

```
ffmpeg -analyzeduration 10M -i rtmp://127.0.0.1/live/$name \
  -f lavfi -i anullsrc=r=48000:cl=stereo \
  -map 0:v:0 -map 0:a:0 -map 1:a:0 -strict -2 \
  -c:a:0 libopus -mapping_family:a:0 255 -b:a:0 1024k -shortest ${FFMPEG_FLAGS} \
  -c:a:1 ${KEEPALIVE_CODEC} -b:a:1 8k -ac:a:1 2 \
  -f dash /opt/data/dash/${DASH_NAME}.mpd
```

That exec line belongs to the RTMP ingress; the direct listeners run their own ffmpeg from the entrypoint's listener block. It is as it stands in `src/nginx-transcoder/nginx-no-ssl.conf`, which is the canonical copy: read the rationale for `-b:a:0 1024k`, for keeping `$name` on `-i`, and for the silent stereo keep-alive output (the `anullsrc` input, the explicit `-map`s, the per-stream `:a:0`/`:a:1` scoping and `-shortest`) only in the comment block directly above that line in [`nginx-transcoder/nginx-no-ssl.conf`](https://github.com/mormegil6/Earshot/blob/master/nginx-transcoder/nginx-no-ssl.conf), rather than repeated here. `nginx.conf` (the `SSL_ENABLED=true` variant) must carry the identical exec line, so diff the two whenever either changes: if they drift, an SSL deployment runs an audio configuration nothing has tested.

16-channel Opus is hardcoded upstream; the video codec policy comes from the `FFMPEG_FLAGS` env var (see [`.env.example`](../../.env.example) at the repo root - `-c:v copy` passthrough by default, VP9 realtime documented as the opt-in codec policy). Every live path also emits a third AdaptationSet alongside the video and the 16-channel Opus programme: a silent stereo keep-alive at 8 kb/s, whose codec follows the container the same `FFMPEG_FLAGS` chooses (AAC in fMP4, Opus on the WebM opt-in). On disk that means `init-stream0/1/2` and `chunk-stream{0,1,2}-NNNNN`. The live MPEG-DASH segmentation happens *here*, not in the shaka service (shaka cannot ingest a 16-channel live stream and is a `tools`-profile utility for VOD packaging only).

HTTP :80 (mapped to host :8081 in dev) serves the webtools monitoring UI at `/webtools`, `rtmp_stat` at `/stat`, a health endpoint at `/` and the raw DASH output at `/dash` (the player normally serves it from the shared volume instead).
