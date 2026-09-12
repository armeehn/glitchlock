# FFglitch 0.10.2 + H.264 CAVLC motion-vector editing

Patches on top of the pristine `ffglitch-0.10.2.tar.xz` tree that let
`ffedit` export and re-apply H.264 motion vectors for CAVLC streams.

    0001  Add direction-only mv export helpers      (ffedit_mv, json.h)
    0002  Add H.264 CAVLC mvd hooks to ffedit       (the feature)
    0003  Enable ffedit for raw H.264 streams       (demuxer flag, codec caps)

`build.sh` reproduces the build; `mvtest.py` is the round-trip test.

## Usage

    ffedit -i in.264 -f mv       -e mv.json        # export
    ffedit -i in.264 -f mv       -a mv.json -o out.264
    ffedit -i in.264 -f mv_delta -e mvd.json       # raw mvd values
    ffedit -i in.264 -o copy.264                    # bit-exact copy

Input and output are raw Annex B (`-f h264` in ffmpeg). Output NALs
carry freshly inserted emulation prevention bytes and may change
length; Annex B has no length fields, so nothing else moves.

## Features

Both names follow the MPEG-4 semantics of FFglitch:

* `mv`: the final vector, `pred + mvd`, in quarter-pel. On import the
  new mvd is `value - pred`, where `pred` is the median prediction
  the decoder computes from the (already edited) neighbours. So the
  vector you write is the vector the decoder ends up with, and
  re-exporting `mv` from the edited file gives back exactly the
  values written.
* `mv_delta`: the raw `mvd_l0` / `mvd_l1` syntax element, exactly the
  se(v) value in the bitstream.

Both are bijective: export -> apply modified -> export returns the
modified values; applying the original JSON onto the edited file
restores the original file byte for byte.

## JSON layout

Same shape as the MPEG-4 `mv` feature, minus `fcode`/`bcode`/`direct`
/`overflow`:

    "mv": {
      "forward":  [ [ null, [x,y], [[x,y],[x,y],[x,y]], ... ], ... ],
      "backward": [ ... ]            # only in frames with B slices
    }

* Outer array: macroblock rows; inner: macroblock columns.
* `forward` = list0 (`mvd_l0`), `backward` = list1 (`mvd_l1`).
* Per macroblock and list: `null` when no mvd is coded for that list
  (I, P_Skip, B_Skip, B_Direct_16x16, or a partition that does not
  use the list), `[x,y]` when exactly one mvd pair is coded, or a list
  of pairs in bitstream order otherwise (16x8/8x16 partitions, then
  8x8 sub-partitions in raster order with their 8x4/4x8/4x4 pieces).
  Direct 8x8 sub-blocks are skipped, so the count per list is the
  number of coded pairs, with no holes.
* I frames still carry an empty `"mv": {}` object, like MPEG-4.
* Frames are keyed by `pkt_pos`, one entry per access unit. Frame
  reordering (B-frames) is handled by ffedit's pkt_pos matching.

On import, a missing/`null` entry keeps the original value; a pair
where a list is expected (or vice versa) is ignored. Values are
clamped to [-32767, 32767] (the se(v) writer's range) with one
warning.

## Design (where the hooks are)

The MPEG-4 path mirrors every bit the decoder reads into a
PutBitContext (`GetBitContext.pb`) and re-encodes edited elements in
place. That does not fit H.264: the decoder works on an RBSP with
emulation prevention bytes stripped, and the CAVLC residual reader
uses raw cache macros that bypass the mirror. Instead:

    libavcodec/ffedit_h264.[ch]   all new code
    libavcodec/h264_cavlc.c       ffe_h264_mb_start() at MB start,
                                  ffe_h264_mvd() replaces the 8
                                  get_se_golomb() mvd reads (16x16,
                                  16x8, 8x16, 8x8 sub-partitions)
    libavcodec/h264_slice.c       ffe_h264_slice_start()/check_slice()
                                  in ff_h264_queue_decode_slice();
                                  ffe_h264_frame_start() after
                                  h264_frame_start()
    libavcodec/h264_picture.c     ffe_h264_frame_end() in
                                  ff_h264_field_end()
    libavcodec/h264dec.c          ffe_h264_nal_done() per NAL in
                                  decode_nal_units(); packet_start/
                                  packet_end wrap h264_decode_frame();
                                  codec caps
    libavcodec/h264dec.h          FFEditH264{,Slice}Context fields
                                  (at the END of the structs: the
                                  first member must stay the AVClass)
    libavformat/h264dec.c         AVFMT_FFEDIT_BITSTREAM|RAWSTREAM

Apply mode:

1. `ffe_h264_mvd()` notes the bit offset and length of each se(v)
   code it reads (`get_bits_count()` before/after) and, when the
   imported value differs, records `{pos, len, new_val}` in the
   slice context.
2. After the slice is decoded, `ffe_h264_nal_done()` rebuilds that
   NAL from its RBSP: copy bits up to each edit, `set_se_golomb()` the
   new value, skip the old code, continue; then stop bit + zero
   alignment (`rbsp_trailing_bits`), then emulation prevention
   (`00 00 0x`, x<=3 -> `00 00 03 0x`) is re-inserted while appending
   to the output packet.
3. NALs without edits, start codes and whatever sits between NALs are
   copied from the input packet by offset (`nal->raw_data`,
   `raw_size`), so unedited output is bit-exact.
4. `ffe_h264_packet_end()` hands the packet to ffedit through
   `avctx->ffe_xp_packets`, the same channel `ffe_transplicate_*`
   uses. The raw demuxer is flagged RAWSTREAM, so ffedit writes
   packets straight through (directwrite) in decode order.

Export/import mode: the JSON objects hang off `cur_pic_ptr->f`
(`ffedit_sd[]`, `jctx`), exactly like `current_picture_ptr->f` for
MPEG-4. `av_frame_ref()` copies the pointers to the output frame.

`ffe_h264_check_slice()` runs after every slice header and exits with
a message when an mv feature is requested on: CABAC
(`pps->cabac`), field pictures or MBAFF, AVCC/length-prefixed input,
or a decoder with more than one slice context. A plain `-o` copy of
such files still works.

## Verified (2026-09-12, LXC 111, gcc 16)

`mvtest.py` replaces every slot with random values in [-64, 64] and
checks: (a) `ffmpeg -v error -i edited.264 -f null -` prints nothing
and the frame count is unchanged; (b) re-export equals the applied
values, same slot count; (c) applying the original JSON on
`edited.264` restores the input byte for byte. Unedited copy and
unchanged re-apply were also `cmp`-identical.

| clip                                   | frames | slots (mv = mv_delta) | result |
|----------------------------------------|-------:|----------------------:|--------|
| cavlc.264 (CBP, 320x240, 2 s)          |     50 |                 6 720 | all pass, both features |
| main_b.264 (Main, bframes=2)           |     50 |                 6 048 | all pass, both features |
| slices4.264 (CBP, slices=4)            |     50 |                 6 937 | all pass |
| real_cavlc.264 (s01e01 30 s, 960x720)  |    720 |               600 932 | all pass, both features |
| real_main_b.264 (same, Main, bframes=2)|    720 |               578 546 | all pass, both features |

B slices: 335 B frames in real_main_b.264, 329 of them export a
`backward` list (the other 6 are all direct/skip); edits to both lists
survive the round trip.

CABAC refusal (`cabac.264`, High profile):

    This H.264 stream uses CABAC (entropy_coding_mode_flag=1).
    FFedit only supports the mv/mv_delta features on CAVLC streams.
    Re-encode with CAVLC (e.g. x264 -x264-params cabac=0).

## Out of scope / known failure modes

* CABAC (High profile default), interlaced/field/MBAFF, AVCC input
  (MP4/MKV): refused when an mv feature is requested.
* HEVC: untouched.
* Only mvd is editable. mb_type, ref_idx, residuals, skip runs stay
  as they are, so the slot layout is fixed by the input stream.
* `mv` values must stay in `int16` once added to the prediction; the
  decoder stores vectors as int16 and wraps silently beyond that. The
  test range (+-64 quarter-pel) is far from that.
* Emulation prevention is re-inserted canonically. A source stream
  with non-canonical (superfluous) 0x03 bytes would round-trip
  bit-exact only for unedited NALs; x264 output is canonical.
* Trailing zero bytes inside a rebuilt NAL (cabac_zero_words, not
  produced for CAVLC) would be dropped.
* If a packet cannot be rebuilt (NAL offsets outside the packet,
  edit ranges out of order, allocation failure) it is copied
  unchanged with a warning "could not be rebuilt".
* `FFEDIT_FLAGS_PARSE_ONLY` is not honoured by the H.264 decoder:
  pixels are fully decoded, so ffedit is as slow as a normal decode
  (~1 s per 720 frames of 960x720 per pass here).
* Frame threading is off (ffedit forces slice threading, default one
  thread); the hooks assume `nb_slice_ctx == 1` and refuse otherwise.
* Built with `--disable-x86asm --disable-libxvid` because LXC 111 has
  no nasm; the bundled Xvid encoder (used by ffgac's libxvid) is
  therefore absent from this build. Everything else matches the
  upstream 0.10.2 configuration minus SDL/xcb/drm (fflive not built).
