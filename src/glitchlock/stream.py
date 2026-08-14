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

from typing import Iterator, List, Optional

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
