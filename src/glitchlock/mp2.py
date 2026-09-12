"""MPEG-1/2 Audio Layer II (MP2): frame parser, writer, and keyed scrambler.

Layer II has no entropy coding. After the 32-bit header (and the optional
16-bit CRC) come four fixed-geometry regions::

    +--------+-----+----------------+-------+--------------+-----------+------+
    | header | crc | bit allocation | scfsi | scalefactors |  samples  | tail |
    | 32 b   | 16b | 2..4 b / band  | 2 b   | 6 b each     | see below | anc. |
    +--------+-----+----------------+-------+--------------+-----------+------+
      never touched (they define the geometry)  ^--- the two lockable regions

The bit allocation says, per subband and channel, how many quantiser levels
each sample has; that fixes the width of every sample field that follows.
Samples with 3, 5 or 9 levels are *grouped*: three samples share one 5-, 7-
or 10-bit codeword whose value is ``s0 + 3*s1 + 9*s2`` (base ``nlevels``).
Every other level count ``2**w - 1`` uses one ``w``-bit field per sample.

So a keyed bijection on each field's *valid range* -- ``nlevels`` for a
plain field, ``nlevels**3`` for a grouped codeword, 63 for a scalefactor
index (63 is forbidden by the spec) -- leaves the frame parseable by every
decoder, sounds like shaped noise, and inverts exactly. XOR would not do: it
can push a value outside the valid range.

The CRC-16, when present, covers header bytes 2-3 plus the bit allocation
and scfsi bits and nothing else (ISO/IEC 11172-3 2.4.3.1; ffmpeg's
``handle_crc`` masks to exactly that many bits). Neither region is touched,
so a locked frame keeps a valid CRC.

Framing is deliberately naive: every valid Layer II header that is followed
by enough bytes for its frame, and whose allocation fits inside it, *is* a
frame. No "does the next header chain" check. The reason is stability: the
scrambler changes sample bits, so any framing decision that peeks into a
frame body could come out differently on the ciphertext and unlock would
frame the file differently from lock. Headers, allocation, scfsi and junk are
never modified, and this rule looks at nothing else. Junk between frames is
carried through verbatim.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, Union

from .transform import LayerStats, apply_plan, plan_frame

CODEC_NAME = "mp2"
FEATURE_SAMPLES = "samples"
FEATURE_SCALEFACTORS = "scalefactors"
#: Lockable features, in the order lock applies them.
FEATURES = (FEATURE_SAMPLES, FEATURE_SCALEFACTORS)

HEADER_BYTES = 4
CRC_BYTES = 2
SYNC_WORD = 0x7FF
SAMPLES_PER_FRAME = 1152
GRANULES = 12
SCALEFACTOR_BITS = 6
#: Scalefactor indices 0..62 are legal; 63 is reserved (11172-3 2.4.2.7).
SCALEFACTOR_LEVELS = 63
SCALEFACTOR_DOMAIN = (0, SCALEFACTOR_LEVELS)
SCFSI_BITS = 2
#: Scalefactors transmitted per (channel, subband), indexed by scfsi code.
SCFSI_COUNT = (3, 2, 1, 2)

VERSION_MPEG2 = 2  # low sampling frequency extension (ISO/IEC 13818-3)
VERSION_MPEG1 = 3
VERSION_NAMES = {VERSION_MPEG1: "MPEG-1", VERSION_MPEG2: "MPEG-2 LSF"}
LAYER_I, LAYER_II, LAYER_III = 3, 2, 1
LAYER_NAMES = {LAYER_I: "Layer I", LAYER_II: "Layer II", LAYER_III: "Layer III"}
MODE_STEREO, MODE_JOINT, MODE_DUAL, MODE_MONO = 0, 1, 2, 3
MODE_NAMES = ("stereo", "joint-stereo", "dual-channel", "mono")
#: Joint stereo: subbands below ``bound`` carry both channels, subbands at
#: or above it share one set of samples (intensity stereo).
JOINT_BOUND_STEP = 4
FREE_FORMAT = 0
RESERVED_BITRATE_INDEX = 15
RESERVED_SAMPLERATE_INDEX = 3
#: Layer II frame length = 144 * bitrate / samplerate, also under LSF.
FRAME_LENGTH_FACTOR = 144000
CRC_POLY = 0x8005
CRC_INIT = 0xFFFF
ID3V2_MAGIC = b"ID3"
MPEG_START_CODE = b"\x00\x00\x01"
SNIFF_BYTES = 4096

#: kbps by bitrate index, per (version, layer). Only the Layer II rows are
#: parsed; the others let :func:`sniff` size a Layer I/III frame.
_LSF_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
BITRATES_KBPS = {
    (VERSION_MPEG1, LAYER_I): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (VERSION_MPEG1, LAYER_II): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (VERSION_MPEG1, LAYER_III): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (VERSION_MPEG2, LAYER_I): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (VERSION_MPEG2, LAYER_II): _LSF_BITRATES,
    (VERSION_MPEG2, LAYER_III): _LSF_BITRATES,
}
SAMPLERATES = {
    VERSION_MPEG1: (44100, 48000, 32000),
    VERSION_MPEG2: (22050, 24000, 16000),
}


class Mp2Error(ValueError):
    pass


# ------------------------------------------------------------ allocation tables

_L_A = (3, 7, 15, 31, 63, 127, 255, 511, 1023, 2047, 4095, 8191, 16383, 32767, 65535)
_L_B = (3, 5, 7, 9, 15, 31, 63, 127, 255, 511, 1023, 2047, 4095, 8191, 65535)
_L_C = (3, 5, 7, 9, 15, 31, 65535)
_L_D = (3, 5, 65535)
_L_E = (3, 5, 9, 15, 31, 63, 127, 255, 511, 1023, 2047, 4095, 8191, 16383, 32767)
_L_F = (3, 5, 9, 15, 31, 63, 127)
_L_G = (3, 5, 7, 9, 15, 31, 63, 127, 255, 511, 1023, 2047, 4095, 8191, 16383)
_L_I = (3, 5, 9)


def _rows(*groups: Tuple[int, Tuple[int, ...]]) -> Tuple[Tuple[int, Tuple[int, ...]], ...]:
    """Expand ``(subband count, level list)`` groups into one ``(nbal,
    levels-by-allocation)`` row per subband. Allocation 0 means no bits."""
    rows: List[Tuple[int, Tuple[int, ...]]] = []
    for count, levels in groups:
        nbal = (len(levels) + 1).bit_length() - 1
        rows.extend([(nbal, (0,) + levels)] * count)
    return tuple(rows)


#: ISO/IEC 11172-3 tables B.2a-d and the ISO/IEC 13818-3 LSF table, in the
#: order ffmpeg's ``ff_mpa_l2_select_table`` numbers them.
ALLOC_TABLES = (
    _rows((3, _L_A), (8, _L_B), (12, _L_C), (4, _L_D)),   # B.2a, sblimit 27
    _rows((3, _L_A), (8, _L_B), (12, _L_C), (7, _L_D)),   # B.2b, sblimit 30
    _rows((2, _L_E), (6, _L_F)),                          # B.2c, sblimit 8
    _rows((2, _L_E), (10, _L_F)),                         # B.2d, sblimit 12
    _rows((4, _L_G), (7, _L_F), (19, _L_I)),              # LSF,  sblimit 30
)

#: Grouped level counts and the width of their three-sample codeword.
GROUPED_WIDTH = {3: 5, 5: 7, 9: 10}


def _quant(levels: int) -> Tuple[int, int, int]:
    """``(field width, valid range, fields per three samples)`` for a level count."""
    if levels in GROUPED_WIDTH:
        return GROUPED_WIDTH[levels], levels ** 3, 1
    return levels.bit_length(), levels, 3


QUANT = {
    levels: _quant(levels)
    for table in ALLOC_TABLES for _nbal, row in table for levels in row if levels
}


def select_table(version: int, samplerate: int, bitrate_kbps: int, channels: int) -> int:
    """Index into :data:`ALLOC_TABLES`, per 11172-3 2.4.2.7 / ffmpeg."""
    if version == VERSION_MPEG2:
        return 4
    per_channel = bitrate_kbps // channels
    if (samplerate == 48000 and per_channel >= 56) or 56 <= per_channel <= 80:
        return 0
    if samplerate != 48000 and per_channel >= 96:
        return 1
    if samplerate != 32000 and per_channel <= 48:
        return 2
    return 3


# ------------------------------------------------------------------- header


@dataclass(frozen=True)
class Header:
    word: int
    version: int
    layer: int
    protection: bool          # True when a CRC-16 follows the header
    bitrate_kbps: int
    samplerate: int
    padding: int
    mode: int
    mode_extension: int
    frame_bytes: int
    channels: int
    table: int
    sblimit: int
    bound: int

    @property
    def raw(self) -> bytes:
        return self.word.to_bytes(HEADER_BYTES, "big")

    @property
    def mode_name(self) -> str:
        return MODE_NAMES[self.mode]


def _header_fields(word: int) -> Optional[Tuple[int, int, int, int, int, int, int, int]]:
    """Split a 32-bit header; None unless sync, version, layer, bitrate and
    samplerate are all legal for *some* MPEG audio layer."""
    if word >> 21 != SYNC_WORD:
        return None
    version = (word >> 19) & 3
    layer = (word >> 17) & 3
    bitrate_index = (word >> 12) & 0xF
    samplerate_index = (word >> 10) & 3
    if version not in VERSION_NAMES or layer == 0:
        return None
    if bitrate_index in (FREE_FORMAT, RESERVED_BITRATE_INDEX):
        return None
    if samplerate_index == RESERVED_SAMPLERATE_INDEX:
        return None
    protection = not (word >> 16) & 1
    padding = (word >> 9) & 1
    mode = (word >> 6) & 3
    mode_extension = (word >> 4) & 3
    return version, layer, bitrate_index, samplerate_index, protection, padding, mode, mode_extension


def parse_header(data: bytes, offset: int = 0) -> Optional[Header]:
    """Decode a Layer II header at *offset*; None if the bytes are not one."""
    if offset + HEADER_BYTES > len(data):
        return None
    word = int.from_bytes(data[offset:offset + HEADER_BYTES], "big")
    fields = _header_fields(word)
    if fields is None:
        return None
    version, layer, bitrate_index, samplerate_index, protection, padding, mode, mode_ext = fields
    if layer != LAYER_II:
        return None
    bitrate = BITRATES_KBPS[(version, LAYER_II)][bitrate_index]
    samplerate = SAMPLERATES[version][samplerate_index]
    channels = 1 if mode == MODE_MONO else 2
    table = select_table(version, samplerate, bitrate, channels)
    sblimit = len(ALLOC_TABLES[table])
    bound = min((mode_ext + 1) * JOINT_BOUND_STEP, sblimit) if mode == MODE_JOINT else sblimit
    return Header(
        word=word,
        version=version,
        layer=layer,
        protection=protection,
        bitrate_kbps=bitrate,
        samplerate=samplerate,
        padding=padding,
        mode=mode,
        mode_extension=mode_ext,
        frame_bytes=FRAME_LENGTH_FACTOR * bitrate // samplerate + padding,
        channels=channels,
        table=table,
        sblimit=sblimit,
        bound=bound,
    )


# ------------------------------------------------------------------- layout

#: Bit strings for every value of every width up to 10, so the writer emits a
#: field with one list index instead of a format() call.
_SMALL_WIDTH = 10
_BITS = [[format(v, f"0{w}b") for v in range(1 << w)] for w in range(_SMALL_WIDTH + 1)]


class _WideBits(dict):
    """Lazily cached bit strings for widths above :data:`_SMALL_WIDTH`."""

    def __init__(self, width: int) -> None:
        super().__init__()
        self._fmt = f"0{width}b"

    def __missing__(self, value: int) -> str:
        text = format(value, self._fmt)
        self[value] = text
        return text


_EMIT: List[Union[List[str], _WideBits]] = [
    _BITS[w] if w <= _SMALL_WIDTH else _WideBits(w) for w in range(17)
]
_EMIT6 = _BITS[SCALEFACTOR_BITS]


@dataclass(frozen=True)
class SampleLayout:
    """Field geometry of one frame's sample region, fixed by the allocation."""
    spans: Tuple[Tuple[int, int], ...]   # (start, end) bit offsets, region-relative
    ranges: Tuple[int, ...]              # valid range of each field
    emitters: Tuple[Union[List[str], _WideBits], ...]
    total_bits: int


_LAYOUTS: Dict[Tuple[int, int, int, Tuple[int, ...]], SampleLayout] = {}


def _sample_layout(header: Header, alloc: List[List[int]]) -> SampleLayout:
    key = (header.table, header.channels, header.bound, tuple(alloc[0]) + tuple(alloc[-1]))
    cached = _LAYOUTS.get(key)
    if cached is not None:
        return cached

    rows = ALLOC_TABLES[header.table]
    # One granule's worth of (width, range) in bitstream order; the other
    # eleven granules repeat it.
    granule: List[Tuple[int, int]] = []
    for sb in range(header.bound):
        for ch in range(header.channels):
            a = alloc[ch][sb]
            if not a:
                continue
            width, span, count = QUANT[rows[sb][1][a]]
            granule.extend([(width, span)] * count)
    for sb in range(header.bound, header.sblimit):
        a = alloc[0][sb]
        if not a:
            continue
        width, span, count = QUANT[rows[sb][1][a]]
        granule.extend([(width, span)] * count)

    fields = granule * GRANULES
    spans: List[Tuple[int, int]] = []
    pos = 0
    for width, _span in fields:
        spans.append((pos, pos + width))
        pos += width
    layout = SampleLayout(
        spans=tuple(spans),
        ranges=tuple(span for _width, span in fields),
        emitters=tuple(_EMIT[width] for width, _span in fields),
        total_bits=pos,
    )
    _LAYOUTS[key] = layout
    return layout


# -------------------------------------------------------------------- frame


@dataclass
class Frame:
    header: Header
    crc: Optional[int]
    alloc: List[List[int]]          # [channel][subband]
    scfsi: List[List[int]]          # [channel][subband], meaningful where alloc != 0
    scalefactors: List[int]         # bitstream order
    samples: List[int]              # bitstream order, one entry per field
    tail: str                       # ancillary bits after the last sample, as '0'/'1'
    layout: SampleLayout = field(repr=False)

    def to_bytes(self) -> bytes:
        return write_frame(self)

    def values(self, feature: str) -> Tuple[List[int], Sequence[int]]:
        """``(values, ranges)`` of one lockable feature. The list is live:
        writing into it edits the frame."""
        if feature == FEATURE_SAMPLES:
            return self.samples, self.layout.ranges
        if feature == FEATURE_SCALEFACTORS:
            return self.scalefactors, (SCALEFACTOR_LEVELS,) * len(self.scalefactors)
        raise Mp2Error(f"unknown MP2 feature {feature!r}; expected one of {FEATURES}")


def parse_frame(raw: bytes, header: Optional[Header] = None) -> Frame:
    """Parse one complete frame. Raises :class:`Mp2Error` on a malformed one."""
    if header is None:
        header = parse_header(raw)
        if header is None:
            raise Mp2Error("not a Layer II frame header")
    if len(raw) != header.frame_bytes:
        raise Mp2Error(f"frame is {len(raw)} bytes, header says {header.frame_bytes}")

    nbits = len(raw) * 8
    bits = format(int.from_bytes(raw, "big"), f"0{nbits}b")
    pos = HEADER_BYTES * 8
    crc = None
    if header.protection:
        crc = int(bits[pos:pos + CRC_BYTES * 8], 2)
        pos += CRC_BYTES * 8

    nch, bound, sblimit = header.channels, header.bound, header.sblimit
    rows = ALLOC_TABLES[header.table]
    channels = range(nch)

    alloc = [[0] * sblimit for _ in channels]
    for sb in range(bound):
        nbal = rows[sb][0]
        for ch in channels:
            alloc[ch][sb] = int(bits[pos:pos + nbal], 2)
            pos += nbal
    for sb in range(bound, sblimit):
        nbal = rows[sb][0]
        shared = int(bits[pos:pos + nbal], 2)
        pos += nbal
        for ch in channels:
            alloc[ch][sb] = shared

    scfsi = [[0] * sblimit for _ in channels]
    for sb in range(sblimit):
        for ch in channels:
            if alloc[ch][sb]:
                scfsi[ch][sb] = int(bits[pos:pos + SCFSI_BITS], 2)
                pos += SCFSI_BITS

    scalefactors: List[int] = []
    for sb in range(sblimit):
        for ch in channels:
            if not alloc[ch][sb]:
                continue
            for _ in range(SCFSI_COUNT[scfsi[ch][sb]]):
                scalefactors.append(int(bits[pos:pos + SCALEFACTOR_BITS], 2))
                pos += SCALEFACTOR_BITS

    layout = _sample_layout(header, alloc)
    end = pos + layout.total_bits
    if end > nbits:
        raise Mp2Error(
            f"allocation needs {end} bits but the frame holds {nbits}"
        )
    region = bits[pos:end]
    samples = [int(region[a:b], 2) for a, b in layout.spans]

    return Frame(
        header=header,
        crc=crc,
        alloc=alloc,
        scfsi=scfsi,
        scalefactors=scalefactors,
        samples=samples,
        tail=bits[end:],
        layout=layout,
    )


def write_frame(frame: Frame) -> bytes:
    """Serialise *frame*. Unedited, this reproduces the parsed bytes exactly."""
    header = frame.header
    parts = [format(header.word, "032b")]
    if header.protection:
        parts.append(format(frame.crc, "016b"))

    nch, bound, sblimit = header.channels, header.bound, header.sblimit
    rows = ALLOC_TABLES[header.table]
    alloc, scfsi = frame.alloc, frame.scfsi
    channels = range(nch)

    for sb in range(bound):
        emit = _EMIT[rows[sb][0]]
        for ch in channels:
            parts.append(emit[alloc[ch][sb]])
    for sb in range(bound, sblimit):
        parts.append(_EMIT[rows[sb][0]][alloc[0][sb]])
    for sb in range(sblimit):
        for ch in channels:
            if alloc[ch][sb]:
                parts.append(_EMIT[SCFSI_BITS][scfsi[ch][sb]])

    parts.extend(map(_EMIT6.__getitem__, frame.scalefactors))
    parts.extend(map(operator.getitem, frame.layout.emitters, frame.samples))
    parts.append(frame.tail)

    text = "".join(parts)
    return int(text, 2).to_bytes(header.frame_bytes, "big")


# ---------------------------------------------------------------------- crc


def crc16(bits: str, crc: int = CRC_INIT) -> int:
    """CRC-16 with generator 0x8005, MSB first, all-ones start (11172-3 2.4.3.1)."""
    for bit in bits:
        msb = crc >> 15
        crc = (crc << 1) & 0xFFFF
        if msb ^ (bit == "1"):
            crc ^= CRC_POLY
    return crc


def protected_bits(frame: Frame) -> str:
    """The bits the CRC covers: header bytes 2-3, bit allocation and scfsi."""
    header = frame.header
    parts = [format(header.word & 0xFFFF, "016b")]
    rows = ALLOC_TABLES[header.table]
    channels = range(header.channels)
    for sb in range(header.bound):
        for ch in channels:
            parts.append(_EMIT[rows[sb][0]][frame.alloc[ch][sb]])
    for sb in range(header.bound, header.sblimit):
        parts.append(_EMIT[rows[sb][0]][frame.alloc[0][sb]])
    for sb in range(header.sblimit):
        for ch in channels:
            if frame.alloc[ch][sb]:
                parts.append(_EMIT[SCFSI_BITS][frame.scfsi[ch][sb]])
    return "".join(parts)


def compute_crc(frame: Frame) -> int:
    return crc16(protected_bits(frame))


def crc_ok(frame: Frame) -> Optional[bool]:
    """True/False for a protected frame, None when it carries no CRC."""
    if not frame.header.protection:
        return None
    return frame.crc == compute_crc(frame)


# ------------------------------------------------------------------ framing

#: A unit of a scanned stream: a parsed frame, or bytes that are not one.
Unit = Union[Frame, bytes]

_FOUND, _NEED_MORE, _EXHAUSTED = 0, 1, 2


def _locate(buf: bytes, pos: int, final: bool) -> Tuple[int, int, Optional[Frame]]:
    """Find the next frame at or after *pos*.

    Returns ``(status, start, frame)``. On ``_FOUND`` the frame begins at
    *start*; bytes between *pos* and *start* are junk. On ``_NEED_MORE``
    (never with *final*) scanning must resume at *start* once more bytes
    arrive; on ``_EXHAUSTED`` everything from *pos* on is junk.
    """
    n = len(buf)
    i = pos
    while True:
        j = buf.find(b"\xff", i)
        if j < 0:
            return _EXHAUSTED, n, None
        if j + HEADER_BYTES > n:
            return (_EXHAUSTED, n, None) if final else (_NEED_MORE, j, None)
        header = parse_header(buf, j)
        if header is None:
            i = j + 1
            continue
        end = j + header.frame_bytes
        if end > n:
            if not final:
                return _NEED_MORE, j, None
            i = j + 1
            continue
        try:
            frame = parse_frame(buf[j:end], header)
        except Mp2Error:
            i = j + 1
            continue
        return _FOUND, j, frame


def iter_units(data: bytes) -> Iterator[Unit]:
    """Yield every frame and every stretch of junk in *data*, in order.
    Concatenating ``unit if bytes else unit.to_bytes()`` rebuilds *data*."""
    pos = 0
    while pos < len(data):
        status, start, frame = _locate(data, pos, final=True)
        if start > pos:
            yield data[pos:start]
        if status != _FOUND:
            return
        assert frame is not None
        yield frame
        pos = start + frame.header.frame_bytes


def iter_frames(data: bytes) -> Iterator[Frame]:
    for unit in iter_units(data):
        if isinstance(unit, Frame):
            yield unit


class FrameReader:
    """Incremental framer: feed it bytes, get whole parsed frames back.

    Mirrors :class:`glitchlock.stream.SegmentReader`. A frame is released as
    soon as its last byte has arrived, so there is no lookahead latency. Bytes
    that belong to no frame are dropped and counted in :attr:`dropped`.

        reader = FrameReader()
        for chunk in socket_reads():
            for frame in reader.feed(chunk):
                handle(frame)
        for frame in reader.flush():
            handle(frame)
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.dropped = 0
        self.frames = 0

    def _drain(self, final: bool) -> List[Frame]:
        out: List[Frame] = []
        buf = bytes(self._buf)
        pos = 0
        while pos < len(buf):
            status, start, frame = _locate(buf, pos, final)
            self.dropped += start - pos
            pos = start
            if status != _FOUND:
                break
            assert frame is not None
            out.append(frame)
            pos = start + frame.header.frame_bytes
        del self._buf[:pos]
        self.frames += len(out)
        return out

    def feed(self, chunk: bytes) -> List[Frame]:
        """Add bytes; return the frames completed by them."""
        self._buf += chunk
        return self._drain(final=False)

    def flush(self) -> List[Frame]:
        """End of stream: release what is complete, drop the rest."""
        out = self._drain(final=True)
        self.dropped += len(self._buf)
        self._buf.clear()
        return out

    @property
    def pending(self) -> int:
        """Bytes held back waiting for the rest of a frame."""
        return len(self._buf)


def iter_mp2_frames(reader, block: int = 65536) -> Iterator[Frame]:
    """Yield frames from a file-like *reader* as they arrive."""
    framer = FrameReader()
    while True:
        chunk = reader.read(block)
        if not chunk:
            break
        for frame in framer.feed(chunk):
            yield frame
    for frame in framer.flush():
        yield frame


# ----------------------------------------------------------------- sniffing


def _skip_id3v2(data: bytes) -> int:
    if not data.startswith(ID3V2_MAGIC) or len(data) < 10:
        return 0
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size


def sniff(data: bytes) -> Optional[str]:
    """``"Layer I"``, ``"Layer II"`` or ``"Layer III"`` when *data* starts an
    MPEG audio elementary stream, else None.

    Needs two consecutive frames with the same version, layer and sampling
    rate within the first few KiB. Anything starting with an MPEG start code
    is video or a program stream, whose muxed audio must not be mistaken for
    an elementary stream.
    """
    if data.startswith(MPEG_START_CODE):
        return None
    start = _skip_id3v2(data)
    window = data[start:start + SNIFF_BYTES]
    i = 0
    while True:
        j = window.find(b"\xff", i)
        if j < 0 or j + HEADER_BYTES > len(window):
            return None
        fields = _header_fields(int.from_bytes(window[j:j + HEADER_BYTES], "big"))
        i = j + 1
        if fields is None:
            continue
        version, layer, bitrate_index, samplerate_index = fields[:4]
        bitrate = BITRATES_KBPS[(version, layer)][bitrate_index]
        samplerate = SAMPLERATES[version][samplerate_index]
        if layer == LAYER_I:
            length = (12000 * bitrate // samplerate + fields[5]) * 4
        elif layer == LAYER_III and version == VERSION_MPEG2:
            length = 72000 * bitrate // samplerate + fields[5]
        else:
            length = FRAME_LENGTH_FACTOR * bitrate // samplerate + fields[5]
        end = start + j + length
        if end == len(data):
            return LAYER_NAMES[layer]
        following = _header_fields(int.from_bytes(data[end:end + HEADER_BYTES], "big"))
        if following is None:
            continue
        if following[:2] == (version, layer) and following[3] == samplerate_index:
            return LAYER_NAMES[layer]


def sniff_file(path: str) -> Optional[str]:
    with open(path, "rb") as fh:
        head = fh.read(SNIFF_BYTES * 8)
    return sniff(head)


def is_mp2(path: str) -> bool:
    return sniff_file(path) == LAYER_NAMES[LAYER_II]


@dataclass
class StreamInfo:
    version: str
    samplerate: int
    mode: str
    channels: int
    bitrate_kbps: int
    frames: int
    protected: int
    junk_bytes: int
    bytes: int

    @property
    def seconds(self) -> float:
        return self.frames * SAMPLES_PER_FRAME / self.samplerate if self.samplerate else 0.0


def describe(data: bytes) -> StreamInfo:
    """Summarise an MP2 elementary stream from its first frame plus a count."""
    info = StreamInfo("", 0, "", 0, 0, 0, 0, 0, len(data))
    for unit in iter_units(data):
        if isinstance(unit, bytes):
            info.junk_bytes += len(unit)
            continue
        header = unit.header
        if not info.frames:
            info.version = VERSION_NAMES[header.version]
            info.samplerate = header.samplerate
            info.mode = header.mode_name
            info.channels = header.channels
            info.bitrate_kbps = header.bitrate_kbps
        info.frames += 1
        info.protected += header.protection
    return info


# ---------------------------------------------------------------- transform

#: One lock layer: ``(feature, mode, intensity)``.
LayerSpec = Tuple[str, str, float]
STREAM_INDEX = 0


def transform_frame(
    frame: Frame,
    key: bytes,
    nonce: bytes,
    frame_idx: int,
    layers: Sequence[LayerSpec],
    forward: bool,
    segment: int = 0,
    stats: Optional[Dict[str, LayerStats]] = None,
) -> Frame:
    """Lock (or unlock) one frame in place and return it.

    Keying matches the video path exactly: the plan for a frame is derived
    from ``(key, nonce, feature, stream 0, frame_idx, segment)`` through
    :func:`glitchlock.transform.plan_frame`, so ``segment`` separates the
    pieces of a chunked stream just as it does for video.
    """
    order = layers if forward else list(reversed(layers))
    for feature, mode, intensity in order:
        values, ranges = frame.values(feature)
        if not values:
            continue
        if forward:
            for i, (value, span) in enumerate(zip(values, ranges)):
                if value >= span:
                    raise Mp2Error(
                        f"frame {frame_idx}: {feature} field {i} holds {value}, "
                        f"outside its legal range 0..{span - 1}; refusing to lock "
                        "a value the transform could not restore"
                    )
        slots = [(values, i, (i,), (0, span)) for i, span in enumerate(ranges)]
        plan = plan_frame(
            slots, key, nonce, feature, STREAM_INDEX, frame_idx, mode, intensity, segment
        )
        touched = apply_plan(slots, plan, forward)
        if stats is not None:
            layer = stats.setdefault(feature, LayerStats())
            layer.frames += 1
            layer.slots_total += len(slots)
            layer.slots_touched += touched
            layer.buckets += len(plan.buckets)
    return frame


def transform_stream(
    data: bytes,
    key: bytes,
    nonce: bytes,
    layers: Sequence[LayerSpec],
    forward: bool,
    segment: int = 0,
) -> Tuple[bytes, Dict[str, LayerStats]]:
    """Lock or unlock a whole elementary stream. Junk passes through untouched."""
    stats: Dict[str, LayerStats] = {feature: LayerStats() for feature, _m, _i in layers}
    out: List[bytes] = []
    frame_idx = 0
    for unit in iter_units(data):
        if isinstance(unit, bytes):
            out.append(unit)
            continue
        transform_frame(unit, key, nonce, frame_idx, layers, forward, segment, stats)
        out.append(unit.to_bytes())
        frame_idx += 1
    return b"".join(out), stats
