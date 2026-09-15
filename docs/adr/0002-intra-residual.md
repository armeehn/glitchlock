# ADR 0002: Residual signs close the I-frame leak (H.264 CAVLC)

Date: 2026-09-15. Status: accepted. Tracker: GLOCK-7, under GLOCK-1.

## Context

Every layer so far edits motion vectors. Intra macroblocks have none, so a
locked stream still shows each I-frame in the clear: a slideshow at GOP
rate, and the P/B frames between them are the true picture with its motion
displaced. The measured leak on the 320x240 `testsrc2` carrier: the
I-frames of an `mv`-locked file are byte-identical to the carrier's
(PSNR inf).

Closing it needs a bitstream element that (1) exists in intra
macroblocks, (2) can be rewritten without changing the length or meaning
of anything parsed after it, and (3) has a fixed slot layout so the
keystream lines up on unlock.

## Options

**A. Quantised levels (`q_dct` style).** Rewriting a level changes its
code length and, through suffixLength adaptation, the codes after it.
Already rejected for MPEG-2 (README, "What does not work"); the same
applies to CAVLC.

**B. Intra prediction modes.** `intra4x4_pred_mode` is coded relative to
the neighbours' modes and with a `prev_intra4x4_pred_mode_flag`; a
permutation changes 1-bit codes into 4-bit codes and back. Reversible in
principle, but the flag makes the slot count data-dependent, and the
legal mode set depends on which neighbours exist. More machinery for a
weaker effect: the residual still carries the texture.

**C. Coefficient signs.** In CAVLC a coefficient's sign lives in one of
two places: a `trailing_ones_sign_flag` (one fixed bit) or the parity
of `levelCode` (`2|level|-2` positive, `2|level|-1` negative). Flipping a
sign keeps `|level|`, so `coeff_token`, `total_zeros`, `run_before` and
the suffixLength adaptation, which depend on `|level|` only, do not move.
The pairs (2m, 2m+1) never straddle a prefix threshold (14, 30 and the
escape boundaries are even), so the code length is unchanged except in
the bare unary case (suffixLength 0, levelCode < 14), where the code
grows or shrinks by one bit. Annex B has no length fields and the NAL
rebuild from patch 0002 already re-encodes ranges of any length.

## Decision

Option C, as FFglitch patch `ffglitch/0005` and feature `q_sign`.

- `decode_residual()` notes where each level's code starts and the
  suffixLength it was read with, and hands the block's levels to
  `ffe_h264_levels()`. Export appends one `0`/`1` per coefficient to the
  macroblock's list, in decode order (luma DC, luma AC or 4x4 blocks,
  chroma DC, chroma AC). Import compares with the JSON and, for each
  changed sign, records a one-bit edit (trailing ones) or a level edit
  re-encoded by `put_level()`, the 9.2.2.1 encoder including the
  `levelCode - 2` rule for the first level after fewer than three
  trailing ones and the prefix >= 16 escape.
- JSON: `"q_sign": {"mb": [[null | [0,1,...], ...], ...]}`. A null
  macroblock is skipped or has no coefficients and yields no slots.
- Python: domain `(0, 2)` per slot; `full` mode XORs a keystream bit and
  permutes signs within the frame. H.264 now locks with `mv` and
  `q_sign` by default; other codecs are unchanged.

## Why it stays decodable

The parser reads exactly the same syntax elements with the same lengths,
so no error path is reachable. The reconstruction is `clip(pred +
residual)`; a flipped coefficient changes pixels, never validity. Stock
`ffmpeg -v error` decodes the locked carriers with empty stderr and the
same frame count.

## Why it is exactly reversible

Each slot is one sign whose position in the macroblock is fixed by
`total_coeff`, which the lock never changes. Unlock re-exports the same
slot layout, inverts the permutation and XOR, and imports the original
signs. The rebuild re-encodes only the edited codes and copies every
other bit, so a level flipped and flipped back is the original code: the
encoder is canonical and so is x264's. Emulation prevention is
re-inserted canonically, as in patch 0002.

Measured (tests/test_qsign.py, LXC 111, ffedit at `/opt/ffglitch-intra`):
unlock is SHA-256 identical to the carrier for the flat test card, a
near-lossless noisy carrier (CRF 2 plus a noise filter: 5.4 million
signs at 320x240, long level codes with prefix 14 and 15), `substitute`
and `permute` alone, `mv` + `q_sign` together, and GOP-segmented
`stream-lock`. Random signs applied through `ffedit` directly re-export
identically and restore the input byte for byte on both carriers.

## Effect

PSNR of the locked stream against the carrier, 320x240 `testsrc2`, 75
frames, GOP 12 closed, two B-frames:

| lock                | I-frames only | all frames |
|---------------------|--------------:|-----------:|
| `mv` (before)       | inf     | 12.82 dB |
| `mv` + `q_sign`     | 7.09 dB   | 7.40 dB |
| `q_sign` alone      | 7.09 dB   | 7.03 dB |

The test pins: I-frame PSNR with `q_sign` at most 15 dB, drop from the
`mv`-only lock at least 30 dB. 5,723 and 7,443 signs in the first two
I-frames, 158,799 in the clip; the `mv` layer has 18,672 slots. `q_sign`
alone scores the same on I-frames as both layers, as it must: `mv` does
not touch them.

## What it does not hide

- Block structure: `mb_type`, `coded_block_pattern`, `total_coeff` per
  block, and every `|level|` stay in the clear. A decoder shows where
  the texture is and how strong, so silhouettes and edges of high-contrast
  content remain guessable from the energy map.
- Motion: `q_sign` alone leaves vectors intact; the `mv` layer covers
  them, and both are on by default for H.264.
- Intra prediction modes and skip runs.
- Headers, timing, GOP structure, resolution.
- CABAC streams (GLOCK-5) and HEVC: refused, as before. HEVC signs are
  bypass bins and would fit the patch 0004 re-encoder; not done here.

## Consequences

- One more FFglitch build: `/opt/ffglitch-intra` in LXC 111 (patches
  0001-0005), CI asset `ffglitch-intra-1`. `/opt/ffglitch-hevc` is left
  for the tools that point at it; run the suite with
  `GLITCHLOCK_FFGLITCH_HOME=/opt/ffglitch-intra`.
- Sign slots outnumber vector slots 8.5 to 1 on this carrier, and each
  slot costs a keystream draw in the Python core: the lock went from
  0.6 s to 2.9 s. A near-lossless noisy 160x120 clip of 1 s has 0.7
  million signs and takes 16 s. Packing sign bits so one draw serves
  many slots is the obvious next step; the 25-minute episode workflow
  (segments in parallel) applies meanwhile.
- A carrier with an older `ffedit` degrades to `mv` only: features are
  intersected with what the binary reports, and the manifest names the
  layers used.
