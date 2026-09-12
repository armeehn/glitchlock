"""MPEG-1/2 Audio Layer II: parser identity, keyed transform, integration.

Fixtures are generated with ffmpeg's own MP2 encoder. ffmpeg never writes a
CRC and never writes joint stereo, so those frames are synthesised here from
its output by re-serialising the parsed fields, then checked against ffmpeg's
decoder (``-err_detect crccheck`` for the CRC).
"""

import io
import math
import os
import random
import shutil
import subprocess
from array import array

import pytest

from glitchlock import cli, mp2
from glitchlock.core import LockError, NoOpLock, lock, lockable_features, select_features, unlock
from glitchlock.crypto import sha256_file
from glitchlock.manifest import Manifest
from glitchlock.transform import plan_frame

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

KEY = bytes(range(32))
NONCE = bytes(range(16))
ALL_LAYERS = [(f, "full", 1.0) for f in mp2.FEATURES]
#: |r| between the original PCM and the locked PCM must stay under this.
MAX_CORRELATION = 0.1
NONCE_SWEEP = 12

SOURCES = {
    "sine": "sine=frequency=440:duration=1",
    "noise": "anoisesrc=color=pink:duration=1:seed=7",
    # Two modulated formants: a speech-like envelope rather than a pure tone.
    "speech": (
        "aevalsrc='sin(2*PI*120*t)*sin(2*PI*600*t)*(0.5+0.5*sin(2*PI*4*t))"
        "|sin(2*PI*130*t)*sin(2*PI*900*t)*(0.5+0.5*sin(2*PI*5*t))':c=stereo:d=1"
    ),
}

#: name -> (source, channels, samplerate, kbps, bitexact). Chosen so every
#: allocation table (B.2a-d and the LSF table) and every legal mode/rate/
#: bitrate corner appears at least once.
MATRIX = {
    "sine-mono-48k-64":       ("sine",   1, 48000, 64,  False),
    "sine-stereo-48k-192":    ("sine",   2, 48000, 192, False),
    "sine-mono-44k-128":      ("sine",   1, 44100, 128, True),
    "noise-stereo-44k-128":   ("noise",  2, 44100, 128, False),
    "noise-mono-32k-64":      ("noise",  1, 32000, 64,  True),
    "noise-stereo-32k-192":   ("noise",  2, 32000, 192, True),
    "noise-stereo-48k-64":    ("noise",  2, 48000, 64,  False),
    "noise-stereo-32k-64":    ("noise",  2, 32000, 64,  False),
    "speech-stereo-48k-256":  ("speech", 2, 48000, 256, False),
    "speech-stereo-44k-384":  ("speech", 2, 44100, 384, True),
    "speech-mono-48k-192":    ("speech", 1, 48000, 192, False),
    "noise-stereo-24k-128":   ("noise",  2, 24000, 128, False),   # MPEG-2 LSF
    "sine-mono-16k-64":       ("sine",   1, 16000, 64,  True),    # MPEG-2 LSF
}


# ----------------------------------------------------------------- helpers


def _ffmpeg(*args):
    proc = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    return proc


def _encode(path, source, channels, rate, kbps, bitexact):
    flags = ["-flags", "+bitexact"] if bitexact else []
    _ffmpeg("-f", "lavfi", "-i", SOURCES[source], "-ac", str(channels), "-ar", str(rate),
            "-c:a", "mp2", "-b:a", f"{kbps}k", *flags, "-f", "mp2", path)


def _decode(path, *extra):
    """``(pcm s16le bytes, stderr text)``; stderr is empty for a clean decode."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", *extra, "-i", path, "-f", "s16le", "-"],
        capture_output=True,
    )
    return proc.stdout, proc.stderr.decode()


def _correlation(a, b):
    xa = array("h", a)
    xb = array("h", b)
    n = min(len(xa), len(xb))
    ma = sum(xa) / n
    mb = sum(xb) / n
    sab = saa = sbb = 0.0
    for x, y in zip(xa, xb):
        dx = x - ma
        dy = y - mb
        sab += dx * dy
        saa += dx * dx
        sbb += dy * dy
    return sab / math.sqrt(saa * sbb)


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _decompose(frame):
    """Scalefactors keyed by ``(sb, ch)`` and, per granule, sample fields
    keyed by ``(sb, ch)``. Shared (joint) subbands are keyed on channel 0."""
    h = frame.header
    rows = mp2.ALLOC_TABLES[h.table]
    sf = {}
    it = iter(frame.scalefactors)
    for sb in range(h.sblimit):
        for ch in range(h.channels):
            if frame.alloc[ch][sb]:
                count = mp2.SCFSI_COUNT[frame.scfsi[ch][sb]]
                sf[(sb, ch)] = [next(it) for _ in range(count)]
    granules = []
    it = iter(frame.samples)
    for _ in range(mp2.GRANULES):
        g = {}
        for sb in range(h.bound):
            for ch in range(h.channels):
                a = frame.alloc[ch][sb]
                if a:
                    g[(sb, ch)] = [next(it) for _ in range(mp2.QUANT[rows[sb][1][a]][2])]
        for sb in range(h.bound, h.sblimit):
            a = frame.alloc[0][sb]
            if a:
                g[(sb, 0)] = [next(it) for _ in range(mp2.QUANT[rows[sb][1][a]][2])]
        granules.append(g)
    return sf, granules


def _assemble(header, alloc, scfsi, sf, granules):
    """Build a frame from decomposed fields, zero-padding the ancillary tail.
    Raises ValueError when the fields do not fit the frame length."""
    nch = header.channels
    rows = mp2.ALLOC_TABLES[header.table]
    sf_list = [
        v for sb in range(header.sblimit) for ch in range(nch)
        if alloc[ch][sb] for v in sf[(sb, ch)]
    ]
    sample_list = []
    for g in granules:
        for sb in range(header.bound):
            for ch in range(nch):
                if alloc[ch][sb]:
                    sample_list.extend(g[(sb, ch)])
        for sb in range(header.bound, header.sblimit):
            if alloc[0][sb]:
                sample_list.extend(g[(sb, 0)])
    layout = mp2._sample_layout(header, alloc)
    assert len(sample_list) == len(layout.spans)

    alloc_bits = sum(rows[sb][0] * (nch if sb < header.bound else 1)
                     for sb in range(header.sblimit))
    scfsi_bits = mp2.SCFSI_BITS * sum(
        1 for sb in range(header.sblimit) for ch in range(nch) if alloc[ch][sb])
    used = (mp2.HEADER_BYTES * 8 + (mp2.CRC_BYTES * 8 if header.protection else 0)
            + alloc_bits + scfsi_bits + mp2.SCALEFACTOR_BITS * len(sf_list)
            + layout.total_bits)
    room = header.frame_bytes * 8 - used
    if room < 0:
        raise ValueError(f"{-room} bits short")
    frame = mp2.Frame(header, 0 if header.protection else None, alloc, scfsi,
                      sf_list, sample_list, "0" * room, layout)
    if header.protection:
        frame.crc = mp2.compute_crc(frame)
    return frame


def _reheader(frame, word):
    header = mp2.parse_header(word.to_bytes(mp2.HEADER_BYTES, "big"))
    assert header is not None
    return header


def _drop_top_subband(alloc, sf, granules, nch):
    sb = max(s for s in range(len(alloc[0])) for ch in range(nch) if alloc[ch][s])
    for ch in range(nch):
        alloc[ch][sb] = 0
        sf.pop((sb, ch), None)
        for g in granules:
            g.pop((sb, ch), None)


def protect(frame):
    """A copy of *frame* carrying a valid CRC-16. ffmpeg fills its frames to
    the last bit, so the top allocated subband is dropped until the 16 bits
    fit."""
    header = _reheader(frame, frame.header.word & ~(1 << 16))
    alloc = [list(a) for a in frame.alloc]
    scfsi = [list(s) for s in frame.scfsi]
    sf, granules = _decompose(frame)
    while True:
        try:
            return _assemble(header, alloc, scfsi, sf, granules)
        except ValueError:
            _drop_top_subband(alloc, sf, granules, header.channels)


def joint_stereo(frame, mode_extension):
    """Rewrite a stereo frame as joint stereo: subbands from ``bound`` up
    take channel 0's allocation and samples for both channels."""
    word = ((frame.header.word & ~(0b1111 << 4))
            | (mp2.MODE_JOINT << 6) | (mode_extension << 4))
    header = _reheader(frame, word)
    alloc = [list(a) for a in frame.alloc]
    scfsi = [list(s) for s in frame.scfsi]
    sf, granules = _decompose(frame)
    for sb in range(header.bound, header.sblimit):
        alloc[1][sb] = alloc[0][sb]
        if alloc[0][sb] and (sb, 1) not in sf:
            scfsi[1][sb] = scfsi[0][sb]
            sf[(sb, 1)] = list(sf[(sb, 0)])
        if not alloc[0][sb]:
            sf.pop((sb, 1), None)
        for g in granules:
            g.pop((sb, 1), None)
    return _assemble(header, alloc, scfsi, sf, granules)


def _rewrite(data, fn):
    return b"".join(u if isinstance(u, bytes) else fn(u).to_bytes()
                    for u in mp2.iter_units(data))


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    d = tmp_path_factory.mktemp("mp2")
    out = {}
    for name, (source, channels, rate, kbps, bitexact) in MATRIX.items():
        path = str(d / f"{name}.mp2")
        _encode(path, source, channels, rate, kbps, bitexact)
        out[name] = path

    # Derived: CRC-protected and joint-stereo variants, and one with junk.
    stereo = _read(out["noise-stereo-44k-128"])
    out["crc-noise-stereo-44k-128"] = _write(str(d / "crc-noise.mp2"), _rewrite(stereo, protect))
    lsf = _read(out["noise-stereo-24k-128"])
    out["crc-noise-stereo-24k-128"] = _write(str(d / "crc-lsf.mp2"), _rewrite(lsf, protect))
    for ext in (0, 1, 2, 3):
        src = _read(out["speech-stereo-48k-256"])
        out[f"joint{ext}-speech-stereo-48k-256"] = _write(
            str(d / f"joint{ext}.mp2"), _rewrite(src, lambda f, e=ext: joint_stereo(f, e)))
    sine = _read(out["sine-stereo-48k-192"])
    out["junk-sine-stereo-48k-192"] = _write(str(d / "junk.mp2"), _with_junk(sine))
    return out


def _with_junk(data):
    """ID3v2 header up front, garbage between two frames, ID3v1 tag at the end."""
    frames = [f.to_bytes() for f in mp2.iter_frames(data)]
    assert len(frames) > 3
    id3 = b"ID3\x04\x00\x00" + bytes([0, 0, 0, 20]) + bytes(20)
    rnd = random.Random(5)
    garbage = bytes(rnd.getrandbits(8) for _ in range(37))
    tag = b"TAG" + bytes(125)
    return id3 + b"".join(frames[:2]) + garbage + b"\xff\xfd" + b"".join(frames[2:]) + tag


FIXTURE_NAMES = list(MATRIX) + [
    "crc-noise-stereo-44k-128", "crc-noise-stereo-24k-128",
    "joint0-speech-stereo-48k-256", "joint1-speech-stereo-48k-256",
    "joint2-speech-stereo-48k-256", "joint3-speech-stereo-48k-256",
    "junk-sine-stereo-48k-192",
]


# ------------------------------------------------------------- the matrix


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_parse_write_identity(fixtures, name):
    data = _read(fixtures[name])
    units = list(mp2.iter_units(data))
    frames = [u for u in units if isinstance(u, mp2.Frame)]
    assert len(frames) > 10
    assert b"".join(u if isinstance(u, bytes) else u.to_bytes() for u in units) == data
    for frame in frames:
        assert mp2.parse_frame(frame.to_bytes()).samples == frame.samples


@pytest.mark.parametrize("name", [n for n in FIXTURE_NAMES if not n.startswith("junk")])
def test_lock_decodes_uncorrelated_and_restores(fixtures, name, tmp_path):
    src = fixtures[name]
    data = _read(src)
    locked, stats = mp2.transform_stream(data, KEY, NONCE, ALL_LAYERS, forward=True)
    assert locked != data
    assert len(locked) == len(data)
    assert stats["samples"].slots_touched > 0
    assert stats["scalefactors"].slots_touched > 0
    locked_path = _write(str(tmp_path / "locked.mp2"), locked)

    pcm, err = _decode(src)
    pcm_locked, err_locked = _decode(locked_path)
    assert err == ""
    assert err_locked == ""
    assert len(pcm_locked) == len(pcm)
    assert abs(_correlation(pcm, pcm_locked)) < MAX_CORRELATION

    restored, _ = mp2.transform_stream(locked, KEY, NONCE, ALL_LAYERS, forward=False)
    assert restored == data


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_verify_sweep_is_exact(fixtures, name):
    rc = cli.main(["verify", fixtures[name], "--repeat", str(NONCE_SWEEP)])
    assert rc == 0


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_segment_changes_ciphertext(fixtures, name):
    data = _read(fixtures[name])
    seg0, _ = mp2.transform_stream(data, KEY, NONCE, ALL_LAYERS, True, segment=0)
    seg1, _ = mp2.transform_stream(data, KEY, NONCE, ALL_LAYERS, True, segment=1)
    seg2, _ = mp2.transform_stream(data, KEY, NONCE, ALL_LAYERS, True, segment=2)
    assert len({seg0, seg1, seg2}) == 3
    back, _ = mp2.transform_stream(seg2, KEY, NONCE, ALL_LAYERS, False, segment=2)
    assert back == data


def test_matrix_covers_every_allocation_table(fixtures):
    seen = set()
    for name in MATRIX:
        frame = next(mp2.iter_frames(_read(fixtures[name])))
        seen.add(frame.header.table)
    assert seen == set(range(len(mp2.ALLOC_TABLES)))


def test_matrix_covers_versions_and_modes(fixtures):
    versions, modes = set(), set()
    for name in FIXTURE_NAMES:
        frame = next(mp2.iter_frames(_read(fixtures[name])))
        versions.add(frame.header.version)
        modes.add(frame.header.mode)
    assert versions == {mp2.VERSION_MPEG1, mp2.VERSION_MPEG2}
    assert modes == {mp2.MODE_STEREO, mp2.MODE_JOINT, mp2.MODE_MONO}


# -------------------------------------------------------------------- crc


def test_crc_matches_ffmpeg(fixtures, tmp_path):
    src = fixtures["crc-noise-stereo-44k-128"]
    data = _read(src)
    frames = list(mp2.iter_frames(data))
    assert all(f.header.protection for f in frames)
    assert all(mp2.crc_ok(f) for f in frames)

    _pcm, err = _decode(src, "-err_detect", "crccheck")
    assert err == ""

    locked, _ = mp2.transform_stream(data, KEY, NONCE, ALL_LAYERS, True)
    locked_path = _write(str(tmp_path / "locked.mp2"), locked)
    assert all(mp2.crc_ok(f) for f in mp2.iter_frames(locked))
    _pcm, err = _decode(locked_path, "-err_detect", "crccheck")
    assert err == ""

    # Negative control: corrupt the CRC field and both sides must notice.
    bad = bytearray(data)
    bad[mp2.HEADER_BYTES] ^= 0x01
    bad_path = _write(str(tmp_path / "bad.mp2"), bytes(bad))
    assert mp2.crc_ok(next(mp2.iter_frames(bytes(bad)))) is False
    _pcm, err = _decode(bad_path, "-err_detect", "crccheck")
    assert "CRC mismatch" in err


def test_crc_lsf(fixtures):
    frames = list(mp2.iter_frames(_read(fixtures["crc-noise-stereo-24k-128"])))
    assert frames[0].header.version == mp2.VERSION_MPEG2
    assert all(mp2.crc_ok(f) for f in frames)
    _pcm, err = _decode(fixtures["crc-noise-stereo-24k-128"], "-err_detect", "crccheck")
    assert err == ""


def test_crc16_known_answer():
    # CRC-16/BUYPASS-style check value for "123456789" with init 0xFFFF, no
    # reflection, no final xor (poly 0x8005) is 0xAEE7.
    bits = "".join(format(b, "08b") for b in b"123456789")
    assert mp2.crc16(bits) == 0xAEE7


# ----------------------------------------------------------- joint stereo


@pytest.mark.parametrize("ext", [0, 1, 2, 3])
def test_joint_stereo_shares_samples_above_bound(fixtures, ext):
    joint = list(mp2.iter_frames(_read(fixtures[f"joint{ext}-speech-stereo-48k-256"])))
    plain = list(mp2.iter_frames(_read(fixtures["speech-stereo-48k-256"])))
    assert len(joint) == len(plain)
    for j, p in zip(joint, plain):
        assert j.header.mode == mp2.MODE_JOINT
        assert j.header.bound == min((ext + 1) * mp2.JOINT_BOUND_STEP, j.header.sblimit)
        assert j.header.bound < j.header.sblimit
        assert len(j.samples) < len(p.samples)
        assert j.alloc[0][j.header.bound:] == j.alloc[1][j.header.bound:]


def test_joint_stereo_decodes_like_the_stereo_source(fixtures):
    pcm_plain, err = _decode(fixtures["speech-stereo-48k-256"])
    pcm_joint, err_joint = _decode(fixtures["joint1-speech-stereo-48k-256"])
    assert err == "" and err_joint == ""
    assert len(pcm_joint) == len(pcm_plain)
    # Same signal, only the top subbands collapsed to one channel.
    assert _correlation(pcm_plain, pcm_joint) > 0.9


def test_dual_channel_is_stereo_geometry(fixtures, tmp_path):
    """Dual channel shares stereo's layout; only the header mode differs."""
    data = _read(fixtures["noise-stereo-44k-128"])
    dual = bytearray()
    for frame in mp2.iter_frames(data):
        header = _reheader(frame, (frame.header.word & ~(0b11 << 6)) | (mp2.MODE_DUAL << 6))
        assert header.bound == header.sblimit and header.channels == 2
        dual += frame.to_bytes().replace(frame.header.raw, header.raw, 1)
    path = _write(str(tmp_path / "dual.mp2"), bytes(dual))
    parsed = list(mp2.iter_frames(bytes(dual)))
    assert all(f.header.mode == mp2.MODE_DUAL for f in parsed)
    assert [f.samples for f in parsed] == [f.samples for f in mp2.iter_frames(data)]
    _pcm, err = _decode(path)
    assert err == ""
    locked, _ = mp2.transform_stream(bytes(dual), KEY, NONCE, ALL_LAYERS, True)
    back, _ = mp2.transform_stream(locked, KEY, NONCE, ALL_LAYERS, False)
    assert locked != dual and back == dual


# ------------------------------------------------------------------ junk


def test_junk_is_carried_verbatim(fixtures, tmp_path):
    src = fixtures["junk-sine-stereo-48k-192"]
    data = _read(src)
    units = list(mp2.iter_units(data))
    junk = [u for u in units if isinstance(u, bytes)]
    assert len(junk) == 3
    assert junk[0].startswith(b"ID3")
    assert junk[-1].startswith(b"TAG")

    locked = str(tmp_path / "locked.mp2")
    restored = str(tmp_path / "restored.mp2")
    result = lock(src, locked, key=KEY, nonce=NONCE, features=list(mp2.FEATURES))
    assert result.manifest.codec == "mp2"
    assert result.manifest.selftest == "pass"
    for j in junk:
        assert j in _read(locked)
    unlock(locked, restored, key=KEY, manifest=result.manifest)
    assert _read(restored) == data
    info = mp2.describe(data)
    assert info.junk_bytes == sum(len(j) for j in junk)


def test_truncated_final_frame_is_junk(fixtures):
    data = _read(fixtures["sine-mono-48k-64"])
    frames = list(mp2.iter_frames(data))
    cut = data[:len(data) - frames[-1].header.frame_bytes // 2]
    units = list(mp2.iter_units(cut))
    assert isinstance(units[-1], bytes)
    assert sum(isinstance(u, mp2.Frame) for u in units) == len(frames) - 1
    locked, _ = mp2.transform_stream(cut, KEY, NONCE, ALL_LAYERS, True)
    back, _ = mp2.transform_stream(locked, KEY, NONCE, ALL_LAYERS, False)
    assert back == cut


# ------------------------------------------------------------- streaming


@pytest.mark.parametrize("chunk", [1, 7, 13, 100, 1000, 65536])
def test_frame_reader_chunk_sizes(fixtures, chunk):
    data = _read(fixtures["junk-sine-stereo-48k-192"])
    expected = [f.to_bytes() for f in mp2.iter_frames(data)]
    reader = mp2.FrameReader()
    got = []
    for i in range(0, len(data), chunk):
        got.extend(f.to_bytes() for f in reader.feed(data[i:i + chunk]))
    got.extend(f.to_bytes() for f in reader.flush())
    assert got == expected
    assert reader.frames == len(expected)
    assert reader.dropped == len(data) - sum(len(f) for f in expected)
    assert reader.pending == 0


def test_frame_reader_sync_split_across_feeds(fixtures):
    data = _read(fixtures["sine-stereo-48k-192"])
    frames = [f.to_bytes() for f in mp2.iter_frames(data)]
    n = len(frames[0])
    reader = mp2.FrameReader()
    assert reader.feed(data[:n + 1]) and reader.pending == 1        # one byte of sync
    assert reader.feed(data[n + 1:n + 3]) == []                      # header still short
    got = reader.feed(data[n + 3:2 * n])
    assert [f.to_bytes() for f in got] == [frames[1]]
    assert reader.pending == 0


def test_iter_mp2_frames_streams_and_unlocks_frame_by_frame(fixtures):
    data = _read(fixtures["noise-stereo-44k-128"])
    locked, _ = mp2.transform_stream(data, KEY, NONCE, ALL_LAYERS, True, segment=3)
    out = []
    for idx, frame in enumerate(mp2.iter_mp2_frames(io.BytesIO(locked), block=333)):
        mp2.transform_frame(frame, KEY, NONCE, idx, ALL_LAYERS, forward=False, segment=3)
        out.append(frame.to_bytes())
    assert b"".join(out) == data


def test_segments_reassemble(fixtures, tmp_path):
    """Cut a stream into pieces, lock each with its own segment number, and
    concatenate: the result unlocks piecewise, the way a receiver would."""
    data = _read(fixtures["speech-mono-48k-192"])
    frames = [f.to_bytes() for f in mp2.iter_frames(data)]
    pieces = [b"".join(frames[i:i + 10]) for i in range(0, len(frames), 10)]
    locked_pieces = []
    for n, piece in enumerate(pieces, start=1):
        src = _write(str(tmp_path / f"p{n}.mp2"), piece)
        dst = str(tmp_path / f"p{n}.locked")
        lock(src, dst, key=KEY, nonce=NONCE, features=list(mp2.FEATURES), segment=n)
        locked_pieces.append(_read(dst))
    stream = b"".join(locked_pieces)
    assert stream != data and len(stream) == len(data)
    restored = []
    for n, piece in enumerate(locked_pieces, start=1):
        back, _ = mp2.transform_stream(piece, KEY, NONCE, ALL_LAYERS, False, segment=n)
        restored.append(back)
    assert b"".join(restored) == data


# ---------------------------------------------------------- modes/features


def test_modes_differ_and_round_trip(fixtures):
    data = _read(fixtures["noise-stereo-44k-128"])
    outputs = {}
    for mode in ("full", "substitute", "permute"):
        layers = [(f, mode, 1.0) for f in mp2.FEATURES]
        locked, _ = mp2.transform_stream(data, KEY, NONCE, layers, True)
        back, _ = mp2.transform_stream(locked, KEY, NONCE, layers, False)
        assert back == data
        outputs[mode] = locked
    assert len(set(outputs.values())) == 3


def test_permute_keeps_the_multiset_per_domain(fixtures):
    data = _read(fixtures["noise-stereo-44k-128"])
    plain = next(mp2.iter_frames(data))
    locked, _ = mp2.transform_stream(data, KEY, NONCE, [("samples", "permute", 1.0)], True)
    shuffled = next(mp2.iter_frames(locked))
    assert shuffled.samples != plain.samples
    assert shuffled.scalefactors == plain.scalefactors
    by_domain = {}
    for v, n in zip(plain.samples, plain.layout.ranges):
        by_domain.setdefault(n, []).append(v)
    for v, n in zip(shuffled.samples, shuffled.layout.ranges):
        by_domain[n].remove(v)
    assert not any(by_domain.values())


def test_feature_subset_leaves_the_other_untouched(fixtures):
    data = _read(fixtures["sine-stereo-48k-192"])
    locked, _ = mp2.transform_stream(data, KEY, NONCE, [("scalefactors", "full", 1.0)], True)
    for a, b in zip(mp2.iter_frames(data), mp2.iter_frames(locked)):
        assert a.samples == b.samples
        assert a.scalefactors != b.scalefactors
        assert a.alloc == b.alloc and a.scfsi == b.scfsi


def test_intensity_touches_a_fraction(fixtures):
    data = _read(fixtures["sine-stereo-48k-192"])
    layers = [("samples", "substitute", 0.25)]
    locked, stats = mp2.transform_stream(data, KEY, NONCE, layers, True)
    touched = stats["samples"].slots_touched / stats["samples"].slots_total
    assert 0.2 < touched < 0.3
    back, _ = mp2.transform_stream(locked, KEY, NONCE, layers, False)
    assert back == data


def test_keying_matches_the_video_label_scheme(fixtures):
    """The audio plan is the video plan: (key, nonce, feature, stream 0,
    frame index, segment) through plan_frame, offsets applied mod range."""
    frame = list(mp2.iter_frames(_read(fixtures["noise-mono-32k-64"])))[5]
    before = list(frame.samples)
    ranges = frame.layout.ranges
    mp2.transform_frame(frame, KEY, NONCE, 5, [("samples", "substitute", 1.0)], True, segment=2)
    slots = [(None, i, (i,), (0, n)) for i, n in enumerate(ranges)]
    plan = plan_frame(slots, KEY, NONCE, "samples", 0, 5, "substitute", 1.0, segment=2)
    assert frame.samples == [(v + k) % n for v, k, n in zip(before, plan.offsets, ranges)]


# ---------------------------------------------------------------- refusals


def test_out_of_range_scalefactor_is_refused(fixtures):
    data = _read(fixtures["sine-mono-48k-64"])
    frame = next(mp2.iter_frames(data))
    frame.scalefactors[0] = mp2.SCALEFACTOR_LEVELS
    bad = frame.to_bytes()
    with pytest.raises(mp2.Mp2Error, match="outside its legal range"):
        mp2.transform_stream(bad, KEY, NONCE, ALL_LAYERS, True)
    # The unlock direction never range-checks: a foreign value is passed through.
    mp2.transform_stream(bad, KEY, NONCE, ALL_LAYERS, False)


def test_all_silent_allocation_is_a_noop(fixtures, tmp_path):
    frame = next(mp2.iter_frames(_read(fixtures["sine-mono-48k-64"])))
    nch = frame.header.channels
    alloc = [[0] * frame.header.sblimit for _ in range(nch)]
    empty = _assemble(frame.header, alloc, [[0] * frame.header.sblimit] * nch, {}, [{}] * mp2.GRANULES)
    src = _write(str(tmp_path / "empty.mp2"), empty.to_bytes() * 4)
    with pytest.raises(NoOpLock):
        lock(src, str(tmp_path / "out.mp2"), key=KEY, nonce=NONCE, features=list(mp2.FEATURES))
    assert not os.path.exists(str(tmp_path / "out.mp2"))


def test_layer_iii_is_refused(tmp_path):
    path = str(tmp_path / "x.mp3")
    _ffmpeg("-f", "lavfi", "-i", SOURCES["sine"], "-c:a", "libmp3lame", "-b:a", "128k",
            "-f", "mp3", "-write_xing", "0", "-id3v2_version", "0", path)
    assert mp2.sniff_file(path) == "Layer III"
    assert not mp2.is_mp2(path)
    with pytest.raises(LockError, match="Layer III is not supported"):
        select_features(path, None)
    assert cli.main(["inspect", path]) == 1


def test_video_and_noise_are_not_sniffed_as_audio(tmp_path):
    video = str(tmp_path / "v.m2v")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=25:duration=0.2",
            "-c:v", "mpeg2video", "-f", "mpeg2video", video)
    assert mp2.sniff_file(video) is None
    rnd = random.Random(1)
    noise = _write(str(tmp_path / "n.bin"), bytes(rnd.getrandbits(8) for _ in range(20000)))
    assert mp2.sniff_file(noise) is None


def test_unknown_feature_is_refused(fixtures):
    with pytest.raises(LockError, match="not an MP2 feature"):
        select_features(fixtures["sine-mono-48k-64"], ["mv"])


# ------------------------------------------------------------------- cli


def test_cli_lock_unlock_with_key_file(fixtures, tmp_path, capsys):
    src = fixtures["speech-stereo-48k-256"]
    key = _write(str(tmp_path / "k"), b"correct horse")
    locked = str(tmp_path / "locked.mp2")
    manifest = str(tmp_path / "m.json")
    restored = str(tmp_path / "back.mp2")
    assert cli.main(["lock", src, "-o", locked, "-m", manifest, "--key-file", key]) == 0
    assert _read(locked) != _read(src)
    m = Manifest.load(manifest)
    assert m.codec == "mp2"
    assert [l.feature for l in m.layers] == list(mp2.FEATURES)
    assert m.selftest == "pass"
    assert m.mac

    assert cli.main(["unlock", locked, "-o", restored, "-m", manifest, "--key-file", key]) == 0
    assert sha256_file(restored) == sha256_file(src)
    assert "byte-exact" in capsys.readouterr().out

    wrong = _write(str(tmp_path / "w"), b"wrong")
    assert cli.main(["unlock", locked, "-o", restored, "-m", manifest, "--key-file", wrong]) == 2


def test_cli_inspect_reports_audio_features(fixtures, capsys):
    assert cli.main(["inspect", fixtures["noise-stereo-44k-128"]]) == 0
    out = capsys.readouterr().out
    assert "codec:    mp2" in out
    assert "44100 Hz, stereo, 128 kbps" in out
    assert "lockable features: samples, scalefactors" in out
    assert lockable_features(fixtures["noise-stereo-44k-128"]) == list(mp2.FEATURES)


def test_cli_permute_mode_and_segment_land_in_manifest(fixtures, tmp_path):
    src = fixtures["noise-mono-32k-64"]
    key = _write(str(tmp_path / "k"), b"k")
    locked = str(tmp_path / "locked.mp2")
    manifest = str(tmp_path / "m.json")
    restored = str(tmp_path / "back.mp2")
    rc = cli.main(["lock", src, "-o", locked, "-m", manifest, "--key-file", key,
                   "--mode", "permute", "--features", "scalefactors", "--segment", "4",
                   "--intensity", "0.5"])
    assert rc == 0
    m = Manifest.load(manifest)
    assert m.segment == 4
    assert [(l.feature, l.mode, l.intensity) for l in m.layers] == [("scalefactors", "permute", 0.5)]
    assert cli.main(["unlock", locked, "-o", restored, "-m", manifest, "--key-file", key]) == 0
    assert _read(restored) == _read(src)


def test_manifest_tamper_is_caught(fixtures, tmp_path):
    src = fixtures["sine-mono-44k-128"]
    key = _write(str(tmp_path / "k"), b"k")
    locked = str(tmp_path / "locked.mp2")
    manifest = str(tmp_path / "m.json")
    assert cli.main(["lock", src, "-o", locked, "-m", manifest, "--key-file", key]) == 0
    m = Manifest.load(manifest)
    m.segment = 9
    m.save(manifest)
    assert cli.main(["unlock", locked, "-o", str(tmp_path / "b"), "-m", manifest,
                     "--key-file", key]) == 2


def test_header_tables():
    assert mp2.select_table(mp2.VERSION_MPEG1, 48000, 192, 2) == 0
    assert mp2.select_table(mp2.VERSION_MPEG1, 44100, 256, 2) == 1
    assert mp2.select_table(mp2.VERSION_MPEG1, 48000, 64, 2) == 2
    assert mp2.select_table(mp2.VERSION_MPEG1, 32000, 64, 2) == 3
    assert mp2.select_table(mp2.VERSION_MPEG2, 24000, 128, 2) == 4
    assert [len(t) for t in mp2.ALLOC_TABLES] == [27, 30, 8, 12, 30]
    assert mp2.QUANT[3] == (5, 27, 1)
    assert mp2.QUANT[5] == (7, 125, 1)
    assert mp2.QUANT[9] == (10, 729, 1)
    assert mp2.QUANT[7] == (3, 7, 3)
    assert mp2.QUANT[65535] == (16, 65535, 3)
    assert mp2.parse_header(b"\xff\xfd\xa4\x04").frame_bytes == 576
    assert mp2.parse_header(b"\xff\xfb\xa4\x04") is None      # Layer III
    assert mp2.parse_header(b"\xff\xfd\x04\x04") is None      # free format
    assert mp2.parse_header(b"\xff\xfd\xf4\x04") is None      # reserved bitrate
