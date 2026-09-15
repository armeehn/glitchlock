"""MPEG-TS transport: lock video and audio inside a transport stream.

A raw elementary stream carries no timestamps, and once B-frames reorder
pictures nothing downstream can rebuild them (ffmpeg 8 refuses to mux raw
H.264 by ``-c copy`` at all). So the transport keeps the container and only
rewrites the bytes inside it::

    in.ts ──demux──▶ video AUs (pts, dts, bytes) ──group by GOP──▶ lock ──┐
          └───────▶ audio PES (pts, bytes)      ────────────────▶ lock ──┼──mux──▶ out.ts
                                                                          │
                     timestamps, PIDs and stream types copied unchanged ──┘

Every access unit keeps its PTS/DTS, so a stock player decodes the locked
stream as garbage in perfect sync, and the key holder gets the carrier's
elementary streams back byte for byte. See docs/adr/0001-transport.md.

Video needs access unit delimiters to be re-split after the lock changes its
length: ffmpeg's TS muxer puts one before every H.264/HEVC access unit, so
those two codecs work; MPEG-1/2/4 in TS do not, yet. MP2 frames keep their
size when locked, so audio is re-split on the original PES boundaries.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from . import mp2
from .core import LockError
from .stream import StreamSession, _work, marker_for

# ----------------------------------------------------------- ISO 13818-1

PACKET_SIZE = 188
SYNC_BYTE = 0x47
PAYLOAD_SIZE = PACKET_SIZE - 4
PID_PAT = 0x0000
PID_NULL = 0x1FFF
#: where this muxer puts the tables and the two elementary streams
PID_PMT = 0x1000
PID_VIDEO = 0x0100
PID_AUDIO = 0x0101
PROGRAM_NUMBER = 1
TRANSPORT_STREAM_ID = 1

TABLE_PAT = 0x00
TABLE_PMT = 0x02
PES_START = b"\x00\x00\x01"
STREAM_ID_AUDIO = 0xC0
STREAM_ID_VIDEO = 0xE0
PTS_ONLY = 0b10
PTS_AND_DTS = 0b11
#: PES header bytes before the optional fields (start code, id, length, 3 flag bytes)
PES_FIXED = 9

STREAM_TYPE_MPEG1_VIDEO = 0x01
STREAM_TYPE_MPEG2_VIDEO = 0x02
STREAM_TYPE_MPEG1_AUDIO = 0x03
STREAM_TYPE_MPEG2_AUDIO = 0x04
STREAM_TYPE_MPEG4_VIDEO = 0x10
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24

CODECS: Dict[int, str] = {
    STREAM_TYPE_MPEG1_VIDEO: "mpeg1video",
    STREAM_TYPE_MPEG2_VIDEO: "mpeg2video",
    STREAM_TYPE_MPEG4_VIDEO: "mpeg4",
    STREAM_TYPE_H264: "h264",
    STREAM_TYPE_HEVC: "hevc",
    STREAM_TYPE_MPEG1_AUDIO: mp2.CODEC_NAME,
    STREAM_TYPE_MPEG2_AUDIO: mp2.CODEC_NAME,
}
AUDIO_TYPES = {STREAM_TYPE_MPEG1_AUDIO, STREAM_TYPE_MPEG2_AUDIO}

#: Annex B start code + access unit delimiter NAL (H.264 type 9, HEVC type 35).
AUD_H264 = b"\x00\x00\x00\x01\x09"
AUD_HEVC = b"\x00\x00\x00\x01\x46\x01"
AU_MARKERS = {"h264": AUD_H264, "hevc": AUD_HEVC}

#: PCR runs this far ahead of the picture it is sent with (90 kHz ticks, 0.5 s),
#: so a player has the packet before its decode time, as ffmpeg's muxdelay does.
PCR_LEAD = 45000
#: video access units between repeats of PAT and PMT (~1 s at 25 fps)
PSI_PERIOD = 25

SESSION_FORMAT = "glitchlock-transport-session"
SESSION_VERSION = "1"
#: audio is one segment; its frames are numbered from the start of the stream
AUDIO_SEGMENT = 0


# ---------------------------------------------------------------- model

@dataclass
class Unit:
    """One PES packet: an access unit for video, one or more frames for audio."""
    pts: Optional[int]
    dts: Optional[int]
    data: bytes


@dataclass
class Track:
    pid: int
    stream_type: int
    units: List[Unit] = field(default_factory=list)

    @property
    def codec(self) -> str:
        return CODECS.get(self.stream_type, f"stream_type_{self.stream_type:#04x}")

    @property
    def is_audio(self) -> bool:
        return self.stream_type in AUDIO_TYPES


@dataclass
class Program:
    tracks: List[Track]

    def _first(self, audio: bool) -> Optional[Track]:
        for t in self.tracks:
            if t.is_audio == audio:
                return t
        return None

    @property
    def video(self) -> Optional[Track]:
        return self._first(audio=False)

    @property
    def audio(self) -> Optional[Track]:
        return self._first(audio=True)


# ---------------------------------------------------------------- bits

def crc32_mpeg(data: bytes) -> int:
    """CRC-32/MPEG-2: poly 0x04C11DB7, init all ones, no reflection, no xorout."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def encode_pts(value: int, prefix: int) -> bytes:
    """33-bit timestamp as the 5-byte PES field: prefix, 3 bits, marker,
    15 bits, marker, 15 bits, marker."""
    value &= (1 << 33) - 1
    return bytes([
        (prefix << 4) | ((value >> 29) & 0x0E) | 1,
        (value >> 22) & 0xFF,
        ((value >> 14) & 0xFE) | 1,
        (value >> 7) & 0xFF,
        ((value << 1) & 0xFE) | 1,
    ])


def _decode_pts(raw: bytes) -> int:
    return (((raw[0] >> 1) & 0x07) << 30) | (raw[1] << 22) | ((raw[2] >> 1) << 15) \
        | (raw[3] << 7) | (raw[4] >> 1)


# ---------------------------------------------------------------- demux

def _packets(data: bytes) -> Iterator[bytes]:
    if len(data) < PACKET_SIZE or data[0] != SYNC_BYTE:
        raise ValueError("not an MPEG transport stream (no 0x47 sync byte)")
    for i in range(0, len(data) - PACKET_SIZE + 1, PACKET_SIZE):
        pkt = data[i:i + PACKET_SIZE]
        if pkt[0] != SYNC_BYTE:
            raise ValueError(f"lost transport stream sync at byte {i}")
        yield pkt


def _payload(pkt: bytes) -> Tuple[int, bool, bytes]:
    """``(pid, payload_unit_start, payload)``; payload is empty when absent."""
    pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
    pusi = bool(pkt[1] & 0x40)
    afc = (pkt[3] >> 4) & 0x03
    start = 4
    if afc & 0x02:
        start += 1 + pkt[4]
    if not afc & 0x01:
        return pid, pusi, b""
    return pid, pusi, pkt[start:]


def _section(payload: bytes) -> bytes:
    """Strip the pointer field and return the PSI section body after the
    length word, without its CRC."""
    body = payload[1 + payload[0]:]
    length = ((body[1] & 0x0F) << 8) | body[2]
    return body[3:3 + length - 4]


def _parse_pat(payload: bytes) -> int:
    body = _section(payload)[5:]
    for i in range(0, len(body), 4):
        program = (body[i] << 8) | body[i + 1]
        pid = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
        if program != 0:
            return pid
    raise ValueError("PAT lists no program")


def _parse_pmt(payload: bytes) -> List[Track]:
    body = _section(payload)[5:]
    info_len = ((body[2] & 0x0F) << 8) | body[3]
    body = body[4 + info_len:]
    tracks = []
    i = 0
    while i + 5 <= len(body):
        stream_type = body[i]
        pid = ((body[i + 1] & 0x1F) << 8) | body[i + 2]
        es_len = ((body[i + 3] & 0x0F) << 8) | body[i + 4]
        tracks.append(Track(pid=pid, stream_type=stream_type))
        i += 5 + es_len
    return tracks


def _parse_pes(raw: bytes) -> Optional[Unit]:
    if not raw.startswith(PES_START) or len(raw) < PES_FIXED:
        return None
    stream_id = raw[3]
    if not (STREAM_ID_AUDIO <= stream_id <= STREAM_ID_VIDEO + 0x0F):
        return None
    length = (raw[4] << 8) | raw[5]
    flags = raw[7] >> 6
    header_len = raw[8]
    pts = dts = None
    if flags & PTS_ONLY:
        pts = _decode_pts(raw[PES_FIXED:PES_FIXED + 5])
    if flags == PTS_AND_DTS:
        dts = _decode_pts(raw[PES_FIXED + 5:PES_FIXED + 10])
    payload = raw[PES_FIXED + header_len:]
    if length:
        payload = payload[:length - 3 - header_len]
    return Unit(pts=pts, dts=dts, data=payload)


def demux(data: bytes) -> Program:
    """Elementary stream units of the first program, with their timestamps."""
    pmt_pid: Optional[int] = None
    tracks: Dict[int, Track] = {}
    buffers: Dict[int, bytearray] = {}

    def close(pid: int) -> None:
        buf = buffers.pop(pid, None)
        if not buf:
            return
        unit = _parse_pes(bytes(buf))
        if unit is not None:
            tracks[pid].units.append(unit)

    for pkt in _packets(data):
        pid, pusi, payload = _payload(pkt)
        if pid == PID_NULL or not payload:
            continue
        if pid == PID_PAT:
            if pmt_pid is None and pusi:
                pmt_pid = _parse_pat(payload)
            continue
        if pid == pmt_pid:
            if not tracks and pusi:
                tracks = {t.pid: t for t in _parse_pmt(payload)}
            continue
        if pid not in tracks:
            continue
        if pusi:
            close(pid)
            buffers[pid] = bytearray()
        if pid in buffers:
            buffers[pid] += payload

    for pid in list(buffers):
        close(pid)
    if not tracks:
        raise ValueError("no PMT found: not a transport stream glitchlock can read")
    return Program(tracks=list(tracks.values()))


# ------------------------------------------------------------------ mux

def _psi(table_id: int, body: bytes) -> bytes:
    """A one-section table in one packet payload: pointer, header, body, CRC,
    0xFF stuffing."""
    length = len(body) + 5 + 4
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF])
    ident = bytes([TRANSPORT_STREAM_ID >> 8, TRANSPORT_STREAM_ID & 0xFF, 0xC1, 0, 0])
    if table_id == TABLE_PMT:
        ident = bytes([PROGRAM_NUMBER >> 8, PROGRAM_NUMBER & 0xFF, 0xC1, 0, 0])
    section = head + ident + body
    section += crc32_mpeg(section).to_bytes(4, "big")
    payload = b"\x00" + section
    return payload + b"\xFF" * (PAYLOAD_SIZE - len(payload))


def _pat() -> bytes:
    body = bytes([PROGRAM_NUMBER >> 8, PROGRAM_NUMBER & 0xFF, 0xE0 | (PID_PMT >> 8), PID_PMT & 0xFF])
    return _psi(TABLE_PAT, body)


def _pmt(tracks: List[Track], pcr_pid: int) -> bytes:
    body = bytes([0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF, 0xF0, 0x00])
    for t in tracks:
        body += bytes([t.stream_type, 0xE0 | (t.pid >> 8), t.pid & 0xFF, 0xF0, 0x00])
    return _psi(TABLE_PMT, body)


def _pes(unit: Unit, video: bool) -> bytes:
    if unit.dts is not None and unit.dts != unit.pts:
        optional = encode_pts(unit.pts, PTS_AND_DTS) + encode_pts(unit.dts, 0b0001)
        flags = PTS_AND_DTS << 6
    elif unit.pts is not None:
        optional = encode_pts(unit.pts, PTS_ONLY)
        flags = PTS_ONLY << 6
    else:
        optional, flags = b"", 0
    stream_id = STREAM_ID_VIDEO if video else STREAM_ID_AUDIO
    # video PES may be unbounded (length 0); audio must carry its length
    length = 0 if video else 3 + len(optional) + len(unit.data)
    if length > 0xFFFF:
        raise ValueError(f"audio PES of {len(unit.data)} bytes does not fit a PES length")
    header = PES_START + bytes([stream_id, length >> 8, length & 0xFF, 0x80, flags, len(optional)])
    return header + optional + unit.data


def _pcr_field(ticks: int) -> bytes:
    """Adaptation field carrying only a PCR: length 7, PCR_flag, base, ext 0."""
    base = max(0, ticks) & ((1 << 33) - 1)
    return bytes([7, 0x10, (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
                  (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E, 0x00])


class _Muxer:
    def __init__(self) -> None:
        self.out = bytearray()
        self.counters: Dict[int, int] = {}

    def packet(self, pid: int, pusi: bool, adaptation: bytes, payload: bytes) -> None:
        cc = self.counters.get(pid, 0)
        self.counters[pid] = (cc + 1) & 0x0F
        afc = (0x02 if adaptation else 0) | (0x01 if payload else 0)
        head = bytes([SYNC_BYTE, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, (afc << 4) | cc])
        pkt = head + adaptation + payload
        assert len(pkt) == PACKET_SIZE, len(pkt)
        self.out += pkt

    def psi(self, pid: int, payload: bytes) -> None:
        self.packet(pid, True, b"", payload)

    def pes(self, pid: int, pes: bytes, pcr: Optional[int]) -> None:
        """Spread one PES packet over TS packets; the tail is padded with an
        adaptation field of stuffing bytes so every packet is 188 long."""
        adaptation = _pcr_field(pcr) if pcr is not None else b""
        pos = 0
        first = True
        while first or pos < len(pes):
            room = PAYLOAD_SIZE - len(adaptation)
            chunk = pes[pos:pos + room]
            pad = room - len(chunk)
            if pad:
                adaptation = _stuff(adaptation, pad)
            self.packet(pid, first, adaptation, chunk)
            pos += len(chunk)
            adaptation = b""
            first = False


def _stuff(adaptation: bytes, pad: int) -> bytes:
    if adaptation:
        return bytes([adaptation[0] + pad]) + adaptation[1:] + b"\xFF" * pad
    if pad == 1:
        return b"\x00"
    return bytes([pad - 1, 0x00]) + b"\xFF" * (pad - 2)


def mux(program: Program) -> bytes:
    """Transport stream for *program*: PAT and PMT up front and every
    :data:`PSI_PERIOD` units of the clock track, PES packets interleaved in
    decode order, PCR on the video PID."""
    tracks = program.tracks
    clock = program.video or tracks[0]
    order = sorted(
        ((u.dts if u.dts is not None else u.pts or 0, ti, ui)
         for ti, t in enumerate(tracks) for ui, u in enumerate(t.units)),
        key=lambda k: (k[0], k[1], k[2]))

    m = _Muxer()
    since_psi = PSI_PERIOD
    for time, ti, ui in order:
        track = tracks[ti]
        if track is clock and since_psi >= PSI_PERIOD:
            m.psi(PID_PAT, _pat())
            m.psi(PID_PMT, _pmt(tracks, clock.pid))
            since_psi = 0
        if track is clock:
            since_psi += 1
        unit = track.units[ui]
        pcr = time - PCR_LEAD if track is clock else None
        m.pes(track.pid, _pes(unit, video=not track.is_audio), pcr)
    return bytes(m.out)


# -------------------------------------------------------------- sessions

@dataclass
class AudioSpec:
    """How the MP2 track is locked. ``features`` empty = audio left in the clear."""
    features: List[str]
    mode: str = "full"
    intensity: float = 1.0

    def layers(self) -> List[mp2.LayerSpec]:
        return [(f, self.mode, self.intensity) for f in self.features]


@dataclass
class TransportSession:
    """Receiver's record: the video stream session plus the audio recipe.
    Holds no key."""
    video: StreamSession
    audio: Optional[AudioSpec] = None
    format: str = SESSION_FORMAT
    version: str = SESSION_VERSION

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump(asdict(self), fh, indent=2, sort_keys=True)
            fh.write("\n")

    @classmethod
    def load(cls, path: str) -> "TransportSession":
        with open(path) as fh:
            data = json.load(fh)
        if data.get("format") != SESSION_FORMAT:
            raise ValueError(f"{path!r} is not a glitchlock transport session")
        if data.get("version") != SESSION_VERSION:
            raise ValueError(f"session version {data.get('version')!r} is not supported")
        audio = AudioSpec(**data["audio"]) if data.get("audio") else None
        return cls(video=StreamSession(**data["video"]), audio=audio,
                   format=data["format"], version=data["version"])


@dataclass
class TransportStats:
    video_segments: int = 0
    audio_frames: int = 0
    frames: int = 0
    slots_touched: int = 0


# ------------------------------------------------------------- pipeline

def _split_aus(data: bytes, marker: bytes, count: int) -> List[bytes]:
    """Cut a locked GOP back into its access units on the delimiters; the
    lock changes bytes inside slices, never the number of pictures."""
    starts = []
    pos = data.find(marker)
    while pos >= 0:
        starts.append(pos)
        pos = data.find(marker, pos + len(marker))
    if starts[:1] != [0] or len(starts) != count:
        raise LockError(f"expected {count} access units after the transform, found {len(starts)}")
    return [data[a:b] for a, b in zip(starts, starts[1:] + [len(data)])]


def _gops(units: List[Unit], marker: bytes, per_segment: int) -> List[List[Unit]]:
    starts = [i for i, u in enumerate(units) if marker in u.data]
    if starts[:1] != [0]:
        raise LockError("video does not begin at a GOP boundary; the transport "
                        "needs every GOP to start with its parameter sets")
    starts = starts[::max(1, per_segment)]
    return [units[a:b] for a, b in zip(starts, starts[1:] + [len(units)])]


def _video(track: Track, session: StreamSession, key: bytes, forward: bool,
           workers: int, stats: TransportStats) -> None:
    au_marker = AU_MARKERS.get(track.codec)
    if au_marker is None:
        raise LockError(f"transport video must carry access unit delimiters "
                        f"(h264, hevc); this stream is {track.codec}")
    if not session.codec:
        session.codec = track.codec
    groups = _gops(track.units, marker_for(track.codec), session.gops)

    def one(number_group: Tuple[int, List[Unit]]) -> List[Unit]:
        number, group = number_group
        out, frames, touched = _work(b"".join(u.data for u in group), number,
                                     session, key, forward)
        stats.frames += frames
        stats.slots_touched += touched
        parts = _split_aus(out, au_marker, len(group))
        return [Unit(u.pts, u.dts, p) for u, p in zip(group, parts)]

    numbered = list(enumerate(groups, start=session.first_segment))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        locked = list(pool.map(one, numbered))
    track.units = [u for group in locked for u in group]
    stats.video_segments = len(groups)


def _audio(track: Track, spec: AudioSpec, key: bytes, nonce: bytes, forward: bool,
           stats: TransportStats) -> None:
    data = b"".join(u.data for u in track.units)
    try:
        out, layer_stats = mp2.transform_stream(data, key, nonce, spec.layers(),
                                                forward, AUDIO_SEGMENT)
    except mp2.Mp2Error as exc:
        raise LockError(f"audio: {exc}") from exc
    if len(out) != len(data):
        raise LockError("audio transform changed the stream length")
    stats.audio_frames = max((s.frames for s in layer_stats.values()), default=0)

    # MP2 frames keep their size, so the PES boundaries are the same offsets
    pos = 0
    for u in track.units:
        u.data = out[pos:pos + len(u.data)]
        pos += len(u.data)


def run_transport(data: bytes, session: TransportSession, key: bytes, forward: bool,
                  workers: int = 4) -> Tuple[bytes, TransportStats]:
    """Lock (``forward``) or unlock every track of a transport stream and
    return the rewritten stream."""
    program = demux(data)
    stats = TransportStats()
    video, audio = program.video, program.audio
    if video is None:
        raise LockError("transport stream has no video track")
    _video(video, session.video, key, forward, workers, stats)
    if audio is not None and session.audio and session.audio.features:
        _audio(audio, session.audio, key, bytes.fromhex(session.video.nonce), forward, stats)
    return mux(program), stats
