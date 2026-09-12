"""Segment helpers for streaming use.

glitchlock's transform is already frame-local: the plan for a frame depends only
on the key, the nonce, the segment number and that frame's own index and
geometry. Nothing carries across frames. So the only thing standing between
glitchlock and a live stream is I/O -- FFedit has to see a complete, decodable
file before it can export anything, and a live stream never ends.

The fix is to cut the stream into pieces FFedit *can* treat as complete files.
In an MPEG-2 elementary stream produced with closed GOPs, every GOP begins with
its own sequence header, so a GOP is exactly that: a self-contained, decodable
unit. Split on sequence headers, lock each piece, concatenate, and the result is
still a valid stream.

Two things matter for correctness:

* **Give every piece its own segment number.** Each piece's frame numbering
  restarts at zero, so without a segment number every piece's frame 0 derives
  the same plan from the same key and nonce. See ``lock(..., segment=n)``.
* **Encode with closed GOPs.** Use ``ffg.transcode(..., closed_gop=True)`` or
  ``glitchlock prepare --closed-gop``. Without it a GOP can reference pictures
  outside itself and will not stand alone.

The receiver needs no side channel: it finds the same boundaries by scanning the
locked stream for the same sequence headers.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, BinaryIO, Callable, Deque, Dict, Iterator, List, Optional, Tuple

from .core import LockError, lock, unlock
from .manifest import Layer, Manifest

#: MPEG-1/2 sequence_header_code. Starts every GOP in a closed-GOP stream.
SEQUENCE_HEADER = b"\x00\x00\x01\xb3"


def segment_offsets(data: bytes, marker: bytes = SEQUENCE_HEADER) -> List[int]:
    """Byte offsets of every segment boundary in *data*."""
    out: List[int] = []
    i = 0
    while True:
        j = data.find(marker, i)
        if j < 0:
            return out
        out.append(j)
        i = j + 1


def split_segments(data: bytes, marker: bytes = SEQUENCE_HEADER) -> List[bytes]:
    """Cut *data* into self-contained segments at each marker.

    Bytes before the first marker are dropped: they are a partial segment from
    before the reader joined, and they cannot be decoded on their own.
    """
    marks = segment_offsets(data, marker)
    if not marks:
        return []
    bounds = marks + [len(data)]
    return [data[bounds[k]:bounds[k + 1]] for k in range(len(marks))]


class SegmentReader:
    """Incremental splitter: feed it bytes, get complete segments back.

    A segment is only complete once the *next* one has started, so this holds
    one segment of latency by construction -- for a 12-frame GOP at 25 fps, half
    a second. Call :meth:`flush` at end of stream to release the final segment.

        reader = SegmentReader()
        for chunk in socket_reads():
            for segment in reader.feed(chunk):
                handle(segment)
        tail = reader.flush()
    """

    def __init__(self, marker: bytes = SEQUENCE_HEADER) -> None:
        self._marker = marker
        self._buf = bytearray()
        self._started = False
        self.dropped_prefix = 0

    def feed(self, chunk: bytes) -> List[bytes]:
        """Add bytes; return any segments that are now complete."""
        self._buf += chunk

        if not self._started:
            first = self._buf.find(self._marker)
            if first < 0:
                # Keep only enough tail to catch a marker split across feeds.
                keep = len(self._marker) - 1
                if len(self._buf) > keep:
                    self.dropped_prefix += len(self._buf) - keep
                    if keep:
                        del self._buf[:-keep]
                    else:
                        self._buf.clear()
                return []
            if first:
                self.dropped_prefix += first
                del self._buf[:first]
            self._started = True

        marks = segment_offsets(bytes(self._buf), self._marker)
        if len(marks) < 2:
            return []

        out = [bytes(self._buf[marks[k]:marks[k + 1]]) for k in range(len(marks) - 1)]
        del self._buf[:marks[-1]]
        return out

    def flush(self) -> Optional[bytes]:
        """Return the final buffered segment, if any."""
        if not self._started or not self._buf:
            return None
        tail = bytes(self._buf)
        self._buf.clear()
        return tail

    @property
    def pending(self) -> int:
        """Bytes currently held back waiting for the next boundary."""
        return len(self._buf)


def iter_file_segments(path: str, block: int = 65536,
                       marker: bytes = SEQUENCE_HEADER) -> Iterator[bytes]:
    """Yield segments from a file, reading it the way a stream would arrive."""
    reader = SegmentReader(marker)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(block)
            if not chunk:
                break
            for segment in reader.feed(chunk):
                yield segment
    tail = reader.flush()
    if tail:
        yield tail


# ------------------------------------------------------------------ sessions

#: MPEG-4 visual_object_sequence_start_code. ``ffgac`` re-emits it before
#: every I-frame, so it plays the role the sequence header plays in MPEG-2.
VOS_HEADER = b"\x00\x00\x01\xb0"

#: Which start code begins a GOP, per codec FFedit can lock.
#: Annex B start code + SPS NAL header (nal_ref_idc 3, type 7). With x264's
#: repeat-headers every IDR access unit opens with one, so it marks a GOP.
SPS_HEADER = b"\x00\x00\x00\x01\x67"

MARKERS = {
    "h264": SPS_HEADER,
    "mpeg2video": SEQUENCE_HEADER,
    "mpeg1video": SEQUENCE_HEADER,
    "mpeg4": VOS_HEADER,
}

SESSION_FORMAT = "glitchlock-stream-session"
SESSION_VERSION = "1"
SNIFF_BYTES = 4096


def marker_for(codec: str) -> bytes:
    try:
        return MARKERS[codec]
    except KeyError:
        raise ValueError(f"no stream marker for codec {codec!r}") from None


def sniff_codec(head: bytes) -> str:
    """Tell MPEG-4 from MPEG-1/2 by whichever GOP start code comes first.

    The splitter needs the marker *before* FFedit has seen a complete file, so
    this is decided from the first bytes rather than from a probe.
    """
    hits = {codec: head.find(m) for codec, m in MARKERS.items()}
    hits = {c: i for c, i in hits.items() if i >= 0}
    if not hits:
        raise ValueError(
            f"no MPEG-1/2 sequence header or MPEG-4 VOS header in the first "
            f"{len(head)} bytes: not an elementary stream glitchlock can split"
        )
    return min(hits, key=hits.get)


@dataclass
class StreamSession:
    """Everything a receiver needs besides the key. Nothing here is secret.

    The transform for a segment depends only on the key, the nonce, the
    segment number and the layer parameters, so a receiver holding this
    record rebuilds each segment's manifest itself. No manifest travels with
    the video.
    """
    codec: str
    nonce: str
    features: List[str]
    mode: str = "full"
    intensity: float = 1.0
    #: number given to the first segment; each following one adds 1
    first_segment: int = 1
    #: GOPs locked together as one segment. More = fewer FFedit spawns per
    #: second, longer latency.
    gops: int = 1
    kdf: Optional[Dict[str, Any]] = None
    format: str = SESSION_FORMAT
    version: str = SESSION_VERSION

    def manifest_for(self, segment: int) -> Manifest:
        """The manifest the lock side would have written for *segment*."""
        layers = [Layer(feature=f, mode=self.mode, intensity=self.intensity)
                  for f in self.features]
        return Manifest(codec=self.codec, nonce=self.nonce, segment=segment,
                        layers=layers, kdf=self.kdf)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")

    @classmethod
    def load(cls, path: str) -> "StreamSession":
        with open(path) as fh:
            data = json.load(fh)
        if data.get("format") != SESSION_FORMAT:
            raise ValueError(f"{path!r} is not a glitchlock stream session")
        if data.get("version") != SESSION_VERSION:
            raise ValueError(f"session version {data.get('version')!r} is not supported")
        return cls(**data)


# ------------------------------------------------------------------ pipeline

@dataclass
class StreamStats:
    segments: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    frames: int = 0
    slots_touched: int = 0


def _group(segments: Iterator[bytes], size: int) -> Iterator[bytes]:
    """Join every *size* GOPs into one segment."""
    batch: List[bytes] = []
    for seg in segments:
        batch.append(seg)
        if len(batch) == size:
            yield b"".join(batch)
            batch = []
    if batch:
        yield b"".join(batch)


def _read_segments(src: BinaryIO, marker: bytes, head: bytes,
                   block: int) -> Iterator[bytes]:
    reader = SegmentReader(marker)
    yield from reader.feed(head)
    while True:
        chunk = src.read(block)
        if not chunk:
            break
        yield from reader.feed(chunk)
    tail = reader.flush()
    if tail:
        yield tail


def _work(data: bytes, number: int, session: StreamSession, key: bytes,
          forward: bool) -> Tuple[bytes, int, int]:
    """Lock or unlock one segment through the file-based core. Returns
    ``(bytes, frames, slots_touched)``."""
    workdir = tempfile.mkdtemp(prefix="glitchlock-stream-")
    try:
        return _work_in(workdir, data, number, session, key, forward)
    except LockError as exc:
        raise LockError(f"segment {number}: {exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _work_in(workdir: str, data: bytes, number: int, session: StreamSession,
             key: bytes, forward: bool) -> Tuple[bytes, int, int]:
    src = os.path.join(workdir, "in.bin")
    dst = os.path.join(workdir, "out.bin")
    with open(src, "wb") as fh:
        fh.write(data)
    if forward:
        res = lock(src, dst, key=key, nonce=bytes.fromhex(session.nonce),
                   features=session.features, mode=session.mode,
                   intensity=session.intensity, selftest=False,
                   segment=number, allow_noop=True)
        repaired = sum(len(l.repairs) for l in res.manifest.layers)
        if repaired:
            # A repair is a per-slot correction stored in the manifest,
            # and in stream mode no manifest reaches the receiver.
            raise LockError(
                f"segment {number} needed {repaired} repairs, which a "
                "stream cannot carry; lock this input as a file instead"
            )
        frames = sum(l.frames for l in res.manifest.layers)
        touched = sum(l.slots_touched for l in res.manifest.layers)
    else:
        unlock(src, dst, key=key, manifest=session.manifest_for(number),
               verify_input=False)
        frames = touched = 0
    with open(dst, "rb") as fh:
        return fh.read(), frames, touched


def run_stream(src: BinaryIO, dst: BinaryIO, session: StreamSession, key: bytes,
               forward: bool, workers: int = 4, block: int = 65536,
               report: Callable[[str], None] = lambda _m: None) -> StreamStats:
    """Pump *src* through lock (``forward``) or unlock into *dst*.

        src ──SegmentReader──▶ seg n ──▶ worker pool ──▶ dst (in order)
                                    (≤ 2×workers in flight)

    Segments are handed to a thread pool but written strictly in arrival
    order, and no more than twice *workers* are in flight, so memory stays
    bounded however fast the input arrives. Latency is one segment plus
    the pool's depth.
    """
    head = src.read(SNIFF_BYTES)
    if forward and not session.codec:
        session.codec = sniff_codec(head)
    marker = marker_for(session.codec)

    stats = StreamStats()
    pending: Deque[Tuple[int, int, Future]] = deque()
    limit = max(1, workers) * 2

    def drain(one: bool) -> None:
        while pending and (one or len(pending) >= limit):
            number, size, fut = pending.popleft()
            try:
                out, frames, touched = fut.result()
            except LockError as exc:
                if not pending and eof:
                    # Only the last piece can be a partial GOP: a stream that
                    # stopped mid-picture, or a file cut with head -c.
                    raise LockError(
                        f"{exc}. This is the final piece, so the stream "
                        "probably ended inside a GOP; everything before it "
                        "was written") from exc
                raise
            dst.write(out)
            stats.segments += 1
            stats.bytes_in += size
            stats.bytes_out += len(out)
            stats.frames += frames
            stats.slots_touched += touched
            report(f"segment {number}: {size} -> {len(out)} bytes")
            if one:
                break

    number = session.first_segment
    eof = False
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        segments = _group(_read_segments(src, marker, head, block), max(1, session.gops))
        for seg in segments:
            drain(one=len(pending) >= limit)
            fut = pool.submit(_work, seg, number, session, key, forward)
            pending.append((number, len(seg), fut))
            number += 1
        eof = True
        while pending:
            drain(one=True)
    dst.flush()
    return stats
