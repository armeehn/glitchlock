"""Streaming: segment splitting, and a full chunked round trip.

The splitter tests are pure and always run. The round trip needs FFglitch and
skips without it.
"""

import subprocess

import pytest

from glitchlock import ffg
from glitchlock.core import lock, select_features, unlock
from glitchlock.crypto import sha256_file
from glitchlock.stream import (
    SEQUENCE_HEADER,
    SegmentReader,
    segment_offsets,
    split_segments,
)

KEY = bytes(range(32))
NONCE = bytes(range(16))

M = SEQUENCE_HEADER


# ----------------------------------------------------------------- splitting


def test_segment_offsets_finds_every_marker():
    data = M + b"aaa" + M + b"bb" + M + b"c"
    assert segment_offsets(data) == [0, 7, 13]


def test_split_segments_are_self_delimiting():
    data = M + b"aaa" + M + b"bb" + M + b"c"
    parts = split_segments(data)
    assert parts == [M + b"aaa", M + b"bb", M + b"c"]
    assert b"".join(parts) == data
    assert all(p.startswith(M) for p in parts)


def test_split_drops_bytes_before_the_first_marker():
    """Joining a live stream mid-GOP: the partial head cannot be decoded."""
    data = b"junk" + M + b"aaa" + M + b"bb"
    assert split_segments(data) == [M + b"aaa", M + b"bb"]


def test_split_with_no_marker_yields_nothing():
    assert split_segments(b"no markers here") == []


def test_reader_matches_split_regardless_of_chunk_sizes():
    data = M + b"aaaaaaa" + M + b"bbbb" + M + b"cc" + M + b"dddddd"
    expected = split_segments(data)
    for size in (1, 2, 3, 5, 7, 64, len(data)):
        reader = SegmentReader()
        got = []
        for i in range(0, len(data), size):
            got += reader.feed(data[i:i + size])
        tail = reader.flush()
        if tail:
            got.append(tail)
        assert got == expected, f"chunk size {size}"


def test_reader_handles_a_marker_split_across_feeds():
    data = M + b"aaa" + M + b"bbb"
    reader = SegmentReader()
    out = reader.feed(data[:6])       # cuts partway into the second marker
    out += reader.feed(data[6:])
    tail = reader.flush()
    if tail:
        out.append(tail)
    assert out == split_segments(data)


def test_reader_holds_one_segment_of_latency():
    """A segment is only complete once the next one begins."""
    reader = SegmentReader()
    assert reader.feed(M + b"aaa") == []      # nothing emitted yet
    assert reader.feed(M) == [M + b"aaa"]     # next marker releases it
    assert reader.pending == len(M)


def test_reader_reports_dropped_prefix():
    reader = SegmentReader()
    reader.feed(b"junkjunk" + M + b"a" + M)
    assert reader.dropped_prefix == 8


# ------------------------------------------------------------- round trip


needs_ffglitch = pytest.mark.skipif(
    not ffg.available(), reason="FFglitch not installed")


@pytest.fixture(scope="module")
def stream_carrier(tmp_path_factory):
    """A closed-GOP carrier, the shape a streaming encoder would emit."""
    d = tmp_path_factory.mktemp("stream")
    src, out = str(d / "src.mp4"), str(d / "carrier.mpg")
    subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=256x192:rate=25:duration=5",
         "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True)
    ffg.transcode(src, out, codec="mpeg2video", gop=12, closed_gop=True)
    return out


@needs_ffglitch
def test_closed_gop_carrier_splits_into_many_segments(stream_carrier):
    data = open(stream_carrier, "rb").read()
    segs = split_segments(data)
    assert len(segs) > 4, "expected several GOP segments"
    assert b"".join(segs) == data


@needs_ffglitch
def test_every_segment_decodes_on_its_own(stream_carrier, tmp_path):
    for i, seg in enumerate(split_segments(open(stream_carrier, "rb").read())):
        p = tmp_path / f"seg{i}.mpg"
        p.write_bytes(seg)
        r = subprocess.run(
            [ffg.ffgac_path(), "-v", "error", "-i", str(p), "-f", "null", "-"],
            capture_output=True, text=True)
        assert r.returncode == 0, f"segment {i} does not decode: {r.stderr[:200]}"


@needs_ffglitch
def test_chunked_stream_roundtrip_is_byte_exact(stream_carrier, tmp_path):
    """Lock segment by segment, concatenate, re-split, unlock. Same bytes."""
    original = open(stream_carrier, "rb").read()
    segments = split_segments(original)
    features = select_features(stream_carrier, None)

    locked_blobs, manifests = [], []
    for n, seg in enumerate(segments):
        src = tmp_path / f"in{n}.mpg"
        dst = tmp_path / f"out{n}.mpg"
        src.write_bytes(seg)
        res = lock(str(src), str(dst), key=KEY, nonce=NONCE, features=features,
                   selftest=False, segment=n + 1)
        locked_blobs.append(dst.read_bytes())
        manifests.append(res.manifest)

    locked_stream = b"".join(locked_blobs)
    assert locked_stream != original

    # the receiver re-derives the boundaries with no side information
    received = split_segments(locked_stream)
    assert len(received) == len(segments)
    assert received == locked_blobs

    restored = []
    for n, blob in enumerate(received):
        src = tmp_path / f"r{n}.mpg"
        dst = tmp_path / f"u{n}.mpg"
        src.write_bytes(blob)
        unlock(str(src), str(dst), key=KEY, manifest=manifests[n],
               verify_input=False)
        restored.append(dst.read_bytes())

    assert b"".join(restored) == original


@needs_ffglitch
def test_locked_stream_still_decodes_end_to_end(stream_carrier, tmp_path):
    features = select_features(stream_carrier, None)
    blobs = []
    for n, seg in enumerate(split_segments(open(stream_carrier, "rb").read())):
        src = tmp_path / f"a{n}.mpg"
        dst = tmp_path / f"b{n}.mpg"
        src.write_bytes(seg)
        lock(str(src), str(dst), key=KEY, nonce=NONCE, features=features,
             selftest=False, segment=n + 1)
        blobs.append(dst.read_bytes())

    joined = tmp_path / "locked_stream.mpg"
    joined.write_bytes(b"".join(blobs))
    r = subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-i", str(joined), "-f", "null", "-"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]


@needs_ffglitch
def test_segments_get_distinct_plans_in_practice(stream_carrier, tmp_path):
    """Two segments with the same content must not lock to the same bytes."""
    segments = split_segments(open(stream_carrier, "rb").read())
    features = select_features(stream_carrier, None)
    src = tmp_path / "s.mpg"
    src.write_bytes(segments[1])

    outs = []
    for seg_no in (1, 2):
        dst = tmp_path / f"s{seg_no}.mpg"
        lock(str(src), str(dst), key=KEY, nonce=NONCE, features=features,
             selftest=False, segment=seg_no)
        outs.append(dst.read_bytes())

    assert outs[0] != outs[1], "same input under different segment numbers must differ"


@needs_ffglitch
def test_ffedit_works_through_pipes(stream_carrier, tmp_path):
    """stdin -> stdout identity apply, which is what a pipeline needs."""
    export = tmp_path / "mv.json"
    subprocess.run(
        [ffg.ffedit_path(), "-v", "error", "-i", "pipe:0", "-f", "mv",
         "-e", str(export)],
        input=open(stream_carrier, "rb").read(), check=True, capture_output=True)
    assert export.stat().st_size > 0

    out = subprocess.run(
        [ffg.ffedit_path(), "-v", "error", "-y", "-i", "pipe:0", "-f", "mv",
         "-a", str(export), "-o", "pipe:1"],
        input=open(stream_carrier, "rb").read(), check=True, capture_output=True)
    piped = tmp_path / "piped.mpg"
    piped.write_bytes(out.stdout)
    assert sha256_file(str(piped)) == sha256_file(stream_carrier)
