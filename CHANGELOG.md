# Changelog

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
