"""Residual-sign layer (``q_sign``) on H.264 CAVLC: closes the I-frame leak.

``mv`` alone leaves every intra frame byte-identical to the carrier, so a
locked stream still shows the picture at GOP rate. ``q_sign`` flips the sign
of every residual coefficient under the key, I-frames included. Numbers and
reasoning in docs/adr/0002-intra-residual.md.

Needs the FFglitch build with patch ffglitch/0005 (``ffedit -i`` lists
``q_sign``); skips otherwise, and CI fails on any skip.
"""

import io
import math
import re
import subprocess

import pytest

from glitchlock import ffg
from glitchlock.core import lock, select_features, unlock
from glitchlock.crypto import sha256_file
from glitchlock.domains import SIGN_DOMAIN, frame_slots
from glitchlock.stream import SPS_HEADER, StreamSession, run_stream

KEY = bytes(range(32))
NONCE = bytes(range(16))

#: Stated margins, measured on the 320x240 testsrc2 carrier (ADR 0002):
#: with ``mv`` only the I-frames are identical to the carrier (PSNR inf);
#: with ``q_sign`` their PSNR against the carrier must fall below this.
IFRAME_PSNR_MAX_DB = 15.0
#: And the drop versus today's lock is at least this much.
IFRAME_PSNR_MIN_DROP_DB = 30.0

_PSNR_AVERAGE = re.compile(r"average:(inf|[0-9.]+)")


def _make_source(path, extra_vf=None, size="320x240", duration=3):
    vf = ["-vf", extra_vf] if extra_vf else []
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size={size}:rate=25:duration={duration}",
         *vf, "-pix_fmt", "yuv420p", path],
        check=True, capture_output=True)


def _carrier(tmp_path_factory, name, extra_vf=None, extra=None, **source):
    if not ffg.available():
        pytest.skip("FFglitch not installed")
    d = tmp_path_factory.mktemp(name)
    src, out = str(d / "src.mp4"), str(d / "carrier.264")
    _make_source(src, extra_vf, **source)
    ffg.transcode(src, out, codec="h264", gop=12, closed_gop=True, extra=extra)
    if "q_sign" not in ffg.supported_features(out):
        pytest.skip("ffedit without q_sign (needs ffglitch/0005)")
    return out


@pytest.fixture(scope="module")
def carrier(tmp_path_factory):
    return _carrier(tmp_path_factory, "qsign")


@pytest.fixture(scope="module")
def noisy_carrier(tmp_path_factory):
    """Near-lossless noisy content: large levels exercise the long CAVLC
    level codes (prefix 14 and 15 with suffixes) that the flat test card
    never reaches. Small and short: every sign is a slot for the Python
    core, and this clip still has about 0.7 million."""
    return _carrier(tmp_path_factory, "qsign_noisy",
                    extra_vf="noise=alls=40:allf=t+u", extra=["-crf", "2"],
                    size="160x120", duration=1)


def decodes_clean(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stderr == ""


def iframe_psnr(path, reference):
    """Average PSNR of the I-frames of *path* against those of *reference*."""
    graph = ("[0:v]select='eq(pict_type\\,I)'[a];"
             "[1:v]select='eq(pict_type\\,I)'[b];[a][b]psnr")
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-i", reference,
                        "-lavfi", graph, "-f", "null", "-"],
                       capture_output=True, text=True)
    m = _PSNR_AVERAGE.search(r.stderr)
    assert m, r.stderr
    return math.inf if m.group(1) == "inf" else float(m.group(1))


def test_q_sign_slots_are_bits_and_skip_null_macroblocks():
    payload = {"mb": [[None, [0, 1, 1]], [[1], None]]}
    slots = frame_slots("q_sign", payload, "h264")
    assert len(slots) == 4
    assert {s[3] for s in slots} == {SIGN_DOMAIN}
    assert slots[0][2] == ("mb", 0, 1, 0)


def test_h264_offers_mv_and_q_sign(carrier):
    assert select_features(carrier, None) == ["mv", "q_sign"]


def test_q_sign_round_trip_is_byte_exact(carrier, tmp_path):
    locked, back = str(tmp_path / "l.264"), str(tmp_path / "u.264")
    res = lock(carrier, locked, key=KEY, nonce=NONCE, features=["q_sign"])
    assert res.manifest.layers[0].slots_touched > 10000
    assert sha256_file(locked) != sha256_file(carrier)
    assert decodes_clean(locked)
    unlock(locked, back, key=KEY, manifest=res.manifest)
    assert sha256_file(back) == sha256_file(carrier)


def test_q_sign_round_trip_on_long_level_codes(noisy_carrier, tmp_path):
    locked, back = str(tmp_path / "l.264"), str(tmp_path / "u.264")
    res = lock(noisy_carrier, locked, key=KEY, nonce=NONCE, features=["q_sign"])
    assert decodes_clean(locked)
    unlock(locked, back, key=KEY, manifest=res.manifest)
    assert sha256_file(back) == sha256_file(noisy_carrier)


@pytest.mark.parametrize("mode", ["substitute", "permute"])
def test_q_sign_modes_round_trip(carrier, tmp_path, mode):
    locked, back = str(tmp_path / "l.264"), str(tmp_path / "u.264")
    res = lock(carrier, locked, key=KEY, nonce=NONCE, features=["q_sign"],
               mode=mode, selftest=False)
    assert sha256_file(locked) != sha256_file(carrier)
    unlock(locked, back, key=KEY, manifest=res.manifest)
    assert sha256_file(back) == sha256_file(carrier)


def test_both_layers_round_trip(carrier, tmp_path):
    locked, back = str(tmp_path / "l.264"), str(tmp_path / "u.264")
    res = lock(carrier, locked, key=KEY, nonce=NONCE,
               features=select_features(carrier, None))
    assert [layer.feature for layer in res.manifest.layers] == ["mv", "q_sign"]
    assert decodes_clean(locked)
    unlock(locked, back, key=KEY, manifest=res.manifest)
    assert sha256_file(back) == sha256_file(carrier)


def test_q_sign_scrambles_iframes_where_mv_does_not(carrier, tmp_path):
    mv_only, with_sign = str(tmp_path / "mv.264"), str(tmp_path / "sign.264")
    lock(carrier, mv_only, key=KEY, nonce=NONCE, features=["mv"], selftest=False)
    lock(carrier, with_sign, key=KEY, nonce=NONCE, features=["mv", "q_sign"],
         selftest=False)

    before = iframe_psnr(mv_only, carrier)
    after = iframe_psnr(with_sign, carrier)
    assert before == math.inf, "mv-only lock should leave I-frames untouched"
    assert after <= IFRAME_PSNR_MAX_DB, after
    assert before - after >= IFRAME_PSNR_MIN_DROP_DB


def test_q_sign_streams_by_gop(carrier):
    original = open(carrier, "rb").read()
    assert original.startswith(SPS_HEADER)
    session = StreamSession(codec="", nonce=NONCE.hex(), features=["q_sign"], gops=2)
    locked = io.BytesIO()
    run_stream(io.BytesIO(original), locked, session, KEY, forward=True, workers=3)
    assert locked.getvalue() != original
    restored = io.BytesIO()
    run_stream(io.BytesIO(locked.getvalue()), restored, session, KEY,
               forward=False, workers=3)
    assert restored.getvalue() == original
