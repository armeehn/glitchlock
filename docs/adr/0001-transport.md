# ADR 0001: MPEG-TS transport keeps the container, rewrites the payload

Date: 2026-09-15. Status: accepted. Tracker: GLOCK-8, unblocks GLOCK-1.

## Context

`stream-lock` works on a raw elementary stream. Delivery needs a container
carrying video and audio together, and four things must hold:

1. Any stock player accepts the locked stream and plays it as garbage, so
   container and timestamp integrity matter.
2. The key holder gets the carrier's elementary streams back byte-exact.
3. Audio and video stay in sync.
4. It is testable in LXC 111 with the FFglitch build present.

Two routes were on the table.

## Options

**A. ffmpeg pipes.** `ffmpeg -c copy -f h264 -` → `stream-lock` →
`ffmpeg -f h264 -i - -i audio -c copy -f mpegts`. Timestamps regenerated
from the frame rate at the second ffmpeg.

Spike (ffmpeg n8.1.2, x264 CAVLC carrier with `bframes=2`, MP2 audio):

- `ffmpeg -r 25 -f h264 -i v.264 -c copy -f mpegts` fails outright:
  `first pts and dts value must be set`, output 0 bytes. `-fflags +genpts`
  does not help. A raw H.264 ES with B-frames has no PTS and ffmpeg 8 no
  longer invents them for `-c copy`. Criterion 1 fails before anything is
  locked.
- The TS muxer inserts an access unit delimiter NAL before every access
  unit, so an ES extracted from a TS is not the ES that went in. Byte
  exactness would have to be argued against ffmpeg's rewriting, not proven.
- Sync would rest on both raw streams starting at zero. True for a file
  from `prepare`, not for anything else.

**B. Python TS demux/mux that keeps PTS.** Parse PAT/PMT and PES, lock the
payload bytes, write the same timestamps back. About 400 lines of stdlib
Python, no ffmpeg in the lock path.

## Decision

Route B. Module `glitchlock.transport`, commands `ts-lock` / `ts-unlock`,
carrier from `prepare --container ts` (system ffmpeg: libx264 or libx265
video, MP2 audio, one call, real encoder timestamps).

- Video: PES packets are access units. Group them into GOPs on the existing
  stream marker (SPS/VPS), lock each GOP through the file core with its
  segment number, re-split on the access unit delimiters the TS muxer
  already put there, reattach each unit's PTS/DTS.
- Audio: MP2 frames keep their byte length when locked, so the whole track
  is transformed and re-cut at the original PES boundaries.
- Mux: PAT, PMT, PES with the copied PTS/DTS, PCR on the video PID, stream
  types and PIDs preserved.

## Consequences

- Locked TS: `ffprobe` and `ffmpeg -f null` accept it with no warnings,
  packet PTS/DTS lists are identical to the carrier's, ES extracted by stock
  ffmpeg from the restored TS equals the carrier's byte for byte (H.264,
  HEVC, MP2). Tests: `tests/test_transport.py`.
- Only H.264 and HEVC video for now: re-splitting needs access unit
  delimiters, which ffmpeg's TS muxer emits for those two and not for
  MPEG-1/2/4. Those are refused with a message. Adding them means a
  picture-start-code splitter, not a design change.
- Whole-file in memory. The demux is a single pass and the lock is per GOP,
  so a streaming variant is an I/O change, not a redesign.
- Audio is one segment numbered from the stream start; a receiver joining
  late has to count frames, which the streaming variant will address.
- As in stream mode, no manifest and no MAC travel with the stream, so a
  wrong key is not detected: `ts-unlock` exits 0 and yields different noise.
- We own a TS muxer. It is minimal (one program, no descriptors, no
  teletext/subtitles) and any input TS is normalised to that shape on output.
