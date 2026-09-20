# Changelog

## Unreleased

- Lock and unlock walk each layer's slots with one digest update per frame
  instead of one address string per slot. `q_sign` on the 320x240 test
  carrier locks in about 60 % of the time; output bytes unchanged.
- `q_sign` layer for H.264 CAVLC (FFglitch patch `ffglitch/0005`): keyed
  flips of every residual coefficient sign, so I-frames are scrambled too
  instead of being left in the clear by `mv`. On by default for H.264,
  byte-exact unlock. Design record in `docs/adr/0002-intra-residual.md`.
- MPEG-TS transport: `prepare --container ts`, `ts-lock`, `ts-unlock`. Video
  (H.264, HEVC) and MP2 audio locked inside the container with PTS/DTS kept,
  so stock players stay in sync and the unlock is byte-exact on both
  elementary streams. Design record in `docs/adr/0001-transport.md`.

## 1.0.0 — 2026-09-13

First public release.

- Keyed, reversible scrambling of motion vectors (`mv`) and quantiser scales
  (`qscale`) with byte-exact unlock; modes `full`, `substitute`, `permute`;
  `--intensity`.
- Carriers: MPEG-1/2 and MPEG-4 part 2 with stock FFglitch 0.10.2; H.264
  (CAVLC) and HEVC with the patched `ffedit` in `ffglitch/` (patches 0001-0004;
  HEVC slices are re-encoded through CABAC). MP2 audio (`samples`,
  `scalefactors`) in pure Python.
- `prepare` builds a carrier from anything ffmpeg reads; `verify --repeat N`
  proves a carrier class; the self-test runs on every lock.
- Keys from a file, a passphrase (scrypt) or the environment; `--keyless`;
  X25519 recipients (`--recipient`, `--identity`).
- Streaming: `stream-lock` / `stream-unlock` over pipes, cut on GOP start codes,
  per-segment keystreams.
- Web front end (`web/`), stdlib-only, with a proof bench.
- 259 tests; CI runs the byte-exact round trips on real bitstreams and fails if
  any of them skip.
