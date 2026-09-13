"""HEVC through the patched FFglitch (see ffglitch/NOTES.md).

Everything here skips without an ffedit that accepts raw HEVC, and says so;
the stock 0.10.2 binary refuses the format outright.
"""

import io
import subprocess

import pytest

from glitchlock import ffg
from glitchlock.core import lock, select_features, unlock
from glitchlock.crypto import sha256_file
from glitchlock.stream import VPS_HEADER, StreamSession, run_stream, split_segments

KEY = bytes(range(32))
NONCE = bytes(range(16))


@pytest.fixture(scope="module")
def carrier(tmp_path_factory):
    if not ffg.available():
        pytest.skip("FFglitch not installed")
    d = tmp_path_factory.mktemp("hevc")
    src, out = str(d / "src.mp4"), str(d / "carrier.265")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=25:duration=3",
         "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True)
    ffg.transcode(src, out, codec="hevc", gop=12, closed_gop=True)
    if "mv" not in ffg.supported_features(out):
        pytest.skip("ffedit without HEVC support (stock FFglitch 0.10.2)")
    return out


def test_carrier_is_main_with_mv(carrier):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=codec_name,profile", "-of", "csv=p=0", carrier],
        capture_output=True, text=True)
    assert r.stdout.strip() == "hevc,Main"
    assert select_features(carrier, None) == ["mv"]


def test_lock_unlock_is_byte_exact_and_scrambles(carrier, tmp_path):
    locked, back = str(tmp_path / "l.265"), str(tmp_path / "u.265")
    res = lock(carrier, locked, key=KEY, nonce=NONCE, features=["mv"], selftest=False)
    assert res.manifest.layers[0].slots_touched > 1000
    assert sha256_file(locked) != sha256_file(carrier)
    # the re-encoded CABAC slices must still be a valid stream
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", locked, "-f", "null", "-"],
                         capture_output=True, text=True)
    assert dec.returncode == 0 and dec.stderr == ""
    unlock(locked, back, key=KEY, manifest=res.manifest)
    assert sha256_file(back) == sha256_file(carrier)


def test_wpp_carrier_is_refused_not_corrupted(tmp_path):
    if not ffg.available():
        pytest.skip("FFglitch not installed")
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "wpp.265")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=25:duration=1",
         "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", src, "-c:v", "libx265",
         "-x265-params", "wpp=1:log-level=error", "-f", "hevc", out],
        check=True, capture_output=True)
    if ffg.codec_name(out) != "hevc":
        pytest.skip("ffedit without HEVC support (stock FFglitch 0.10.2)")
    with pytest.raises(Exception) as err:
        ffg.export(out, "mv", str(tmp_path / "mv.json"))
    assert "wavefront" in str(err.value)


def test_stream_splits_on_vps_and_round_trips(carrier):
    original = open(carrier, "rb").read()
    assert original.startswith(VPS_HEADER)
    assert len(split_segments(original, VPS_HEADER)) > 4

    session = StreamSession(codec="", nonce=NONCE.hex(), features=["mv"], gops=2)
    locked = io.BytesIO()
    run_stream(io.BytesIO(original), locked, session, KEY, forward=True, workers=3)
    assert session.codec == "hevc"
    assert locked.getvalue() != original

    restored = io.BytesIO()
    run_stream(io.BytesIO(locked.getvalue()), restored, session, KEY,
               forward=False, workers=3)
    assert restored.getvalue() == original
