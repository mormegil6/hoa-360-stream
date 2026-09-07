# Ambisonic order: why 16 channels, and what it would take to go higher

Where the ceiling actually sits, what is already verified above it, and the two routes past it. Summarised in three sentences in the [main README](../README.md#architecture); this is the whole argument.

## Why 16 channels

In practice, on the FFmpeg 7.1 that `earshot` pins: 1st order (4 ch) and 3rd order (16 ch) work end to end with no special handling, because `quad` and `hexadecagonal` are layouts its AAC encoder has a PCE configuration for; 2nd order (9 ch) must be zero-padded to 16 by the sender, because 9 has no such configuration on any released FFmpeg (see [What changed upstream, and what has not shipped](#what-changed-upstream-and-what-has-not-shipped)). The ceiling sits on the contribution leg, not on delivery or rendering. ffmpeg's AAC encoder refuses a 25-channel (4th-order) input outright - 16 works only because `hexadecagonal` is a *named* layout it accepts - and that leg has to be AAC because RTMP/FLV cannot carry Opus. AAC's standard channel configurations stop at 7.1, so anything wider has to spell its layout out in a Program Config Element (PCE) instead of naming one - and ffmpeg writes a PCE only for the layouts in its own table, which is why `hexadecagonal` works and 25 channels does not. `earshot` pins ffmpeg 7.1, built from the checksum-verified release tarball, so the image does not inherit whatever the host has; that pin now also matters in the other direction, since FFmpeg 9.0 dropped `hexadecagonal` from that table and no release has yet shipped the ambisonic entries that replace it. Everything downstream is already order-4 capable, verified component by component: 25-channel Opus at `mapping_family 255` round-trips intact, Shaka Packager carries `AudioChannelConfiguration value="25"` into the manifest, and the player image ships the complete order-4 impulse-response set.

## The on-demand path is already 4th order

So **the on-demand path is 4th-order capable, verified end to end** - it never touches AAC, and the player reads the ambisonic order from the manifest, so a 25-channel clip plays as 4th order with no configuration. A synthetic 25-channel clip packaged by the same DASH tooling has been played through the full chain: auto-detected as order 4, rendered through the complete order-4 impulse-response set, audio and video clocks in sync. A real recording would sound different but exercise the identical code path, so nothing here is waiting on content.

## Raising the live path: two candidate routes

Raising the *live* path is a different matter, with two candidate routes. One stays on RTMP/AAC and gives up a channel. AAC's widest named layout is 22.2 (24 channels - encode, FLV mux and read-back verified on this stack's own ffmpeg), which fits if the 4th-order vertical harmonic (ACN 20) is dropped. Two Big Ears made the same perceptual trade in their 8-channel TBE format, whose published mapping simply does not carry the 2nd-order vertical harmonic (ACN 6) (see [Farina's channel-by-channel conversion](https://www.angelofarina.it/TBE-conversion.htm)); here, the order 1-3 vertical components (ACN 2, 6, 12) all survive. The other moves contribution off RTMP to SRT carrying **Opus** in MPEG-TS, which would delete the AAC transcode and with it the ceiling. That half is done: with `SRT_DIRECT=1` the gateway hands the caller's mpegts straight to `earshot`, deleting the AAC re-encode and the RTMP hop. It does not lift the ceiling, and the reason has MOVED: the direct listeners are fixed-layout, joining 4x4 to hexadecagonal or passing a single quad through (one port per shape and video codec, :9100 to :9103), and the gateway's probe accepts only those two shapes, so a wider stream is refused before any codec question arises. `GUEST_SRT_DIRECT=1` is the default too, so both routes reach `earshot` directly. The 16-channel AAC-over-RTMP leg survives as the `GUEST_SRT_DIRECT=0` fallback, where it exists so the whole guest arbiter applies to the session unchanged. So SRT is here and the AAC hop is out of both defaults, and the ceiling that remains is the listener shape above, which is architectural rather than configuration.

The sender side has moved, though. Multitrack SRT already carries 16 channels from stock OBS as four 4-channel tracks, and OBS allows six tracks. Thus 4th order (25 ch) would fit as five tracks of 5.1, five usable channels each once the muted LFE slot is dropped. **Live 4th order is therefore theoretically reachable, but untested as of yet.** It needs two independent things - that wider sender layout, and a direct listener that accepts a wider shape than the two it has today (4x4 and 1x4). Neither route funnels through 16-channel AAC over RTMP by default any more, so that half is done; the fixed listener layouts are what remain.

## Beyond 3rd order (sender side)

Noted, not built. The same per-track pattern extends past 3rd order. Six tracks of 5.1 (six channels each, five usable, so 30) comfortably covers 4th order (25 ch, with headroom); six tracks of 7.1 (eight each, seven usable, so 42) would cover 5th order (36 ch, `(5+1)^2`, with headroom - HOAST360's renderer is reportedly capable of it, though no impulse-response set for it ships in the publicly available version). The `.1`/LFE slot is never usable, which is why the counts above discount it: OBS mutes it outright, measured digital-silent on 7.1 (2026-07-31) and on 5.1 (2026-08-08, -90 dBFS on a real capture). There is no LFE-free surround layout to escape to either, stock OBS offering only Mono, Stereo, 2.1, 4.0, 4.1, 5.1 and 7.1 - and ambisonics has no channel that maps onto "LFE" anyway. Note this is the *sender* side only. Both routes now hand MPEG-TS to `earshot` directly, but the listeners are fixed at 4x4 and 1x4, so raising the live order still needs a listener that accepts the wider shape. 4.0 stays the actual recipe for now. [docs/obs-macos.md](obs-macos.md) and [docs/obs-windows.md](obs-windows.md) cover how the per-track joining works today.

## Channel counts

The pipeline is order-flexible where the tools allow it. `earshot`'s transcode carries any channel count into `mapping_family 255` Opus, and the player reads the ambisonic order from the `AudioChannelConfiguration` of the manifest's Opus AdaptationSet (4 ch = 1st order, 16 ch = 3rd order; verified end to end for both), and never from whichever audio set the muxer emitted first, because the live manifest also carries the silent stereo keep-alive set at `value="2"`. The hard limit sits in the RTMP contribution leg: ffmpeg's AAC encoder writes a PCE only for the layouts in its own static table, so on the pinned 7.1 that is 4 (`quad`) and 16 (`hexadecagonal`), while 9 (2nd order) and 25 (4th order) are refused outright; a 2nd-order source must be zero-padded to 16 channels by the sender (a valid 3rd-order signal with silent upper orders). Master has since gained a 9-channel ambisonic entry that no release carries, and 25 was not added at all: [What changed upstream, and what has not shipped](#what-changed-upstream-and-what-has-not-shipped).

### Why 2nd order pads to 16 rather than to 10

A reasonable idea, once you know modern ffmpeg: send 2nd order as a 10-channel named layout such as `5.1.4` and waste one channel instead of seven. It does not work here, and the reason is the **vendored ffmpeg's age**, not the format.

`earshot` builds ffmpeg 7.1 from the checksum-verified release tarball (`FFMPEG_VERSION=7.1`). Two lists matter here and they are not the same one: the layouts ffmpeg can *name*, and the shorter set its AAC encoder can *encode*. Measured in the shipped image, from 6 channels up:

| Channels | Named in the image | Accepted by the AAC encoder |
|---|---|---|
| 6 to 8 | `5.1`, `6.0`, `hexagonal`, `6.1`, `7.0`, `7.1`, `5.1.2`, `octagonal`, `cube` | yes |
| 9 | none | - |
| 10 to 14 | `5.1.4`, `7.1.2`, `7.1.4`, `7.2.3`, `9.1.4` | **no**: `Unsupported channel layout` |
| 16 | `hexadecagonal` | yes |
| 24 | `22.2` | yes |

So 2nd order still has nowhere to land, but not for the reason it is easy to assume. The height layouts exist in this build; the AAC encoder simply has no PCE configuration for them and refuses them outright. Nine channels therefore go out zero-padded to 16, and that is the only reachable layout rather than a design preference. Both gaps are closed on FFmpeg master and in no release, which is the next subsection.

Check the encoder, not the layout list, before designing around a channel count:

```bash
docker run --rm --entrypoint ffmpeg ambi-box-earshot:local -hide_banner -layouts
```

A newer ffmpeg does not lift this by itself: 7.1 names the height layouts and still refuses to encode them.

#### What changed upstream, and what has not shipped

FFmpeg master added AAC PCE configurations for the height layouts (`ed923a7a89`) and for ambisonic layouts at 4, 9 and 16 channels (`f35acb72ac`) on 2026-08-23, closing [FFmpeg#24218](https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/24218), but no release carries them - 9.0.1 of 2026-08-12 predates the fix - and `earshot` pins 7.1, so 9 channels stays refused on every version this stack can run, and 25 was not added at all.

The 9-channel entry carries this project's own contribution: the reorder map `{2,5,6,0,1,7,8,3,4}` was measured here and adopted verbatim ([docs/UPSTREAM.md](UPSTREAM.md)). When a release does carry it, 2nd order gains a native 9-channel AAC path and the zero-padding above becomes a compatibility measure rather than the only option. Nothing in that work reaches 4th order.

**Over SRT, 1st and 3rd order both work, and the gateway picks between them for you.** `srt-gateway` buffers the start of the stream, asks ffprobe what is in it, and chooses: four 4-channel tracks are joined into one `hexadecagonal` stream (3rd order), while a single 4-channel track is already `quad` and passes straight through (1st order). Nothing is configured at either end. Anything else is refused within a few seconds with the reason, rather than being accepted and then producing no output. So a 1st-order microphone, which is what most ambisonic rigs are, works on the recommended route as one 4-channel track. A plain stereo or mono push produces no output at all and is auto-ended on the guest endpoint with that reason.

## Independent corroboration: a Quest 3 decodes both layouts

The `?dbg` capability probe on the VOD page (see the URL-flags note in [docs/ENDPOINTS.md](ENDPOINTS.md)) reports what a browser actually managed. On a Meta Quest 3 (2026-07-27):

<div align="center"> <img src="images/quest3-browser-capability.jpg" width="85%" alt="The VOD page open in a Meta Quest 3 browser at stream.bmroz.eu/vod/?dbg, showing the 360 test card rendered with the ambisonic energy overlay, and a diagnostic panel reporting that 2-, 16- and 25-channel Opus all decoded"> </div>

<p align="center"><em>The <code>?dbg</code> probe on a Meta Quest 3 (2026-07-27). The line that matters for order is <strong>25-channel Opus decoding on the headset itself</strong>: the delivery and playback end of a 4th-order chain is not the part that is missing. The same photograph appears in the <a href="../README.md#what-it-looks-like-running">main README</a>, where it stands for something simpler, that the stack runs on the device it is built for.</em></p>

```
Stereo Opus control (WebM):    DECODED (2 ch, 48000 Hz, 1 s)
16-channel (3OA) Opus (WebM):  DECODED (16 ch, 48000 Hz, 1 s)
25-channel (4OA) Opus (WebM):  DECODED (25 ch, 48000 Hz, 1 s)
```

Both the 3rd-order and the 4th-order layouts decode there, which is independent corroboration of the order-4 claim above from consumer hardware and a different browser engine (OculusBrowser 149) than the headless harness used for the end-to-end test. `AudioContext maxChannelCount: 2` in the same panel is the headset's *output* device being stereo, which is exactly right for binaural rendering; it is not a limit on what can be decoded.
