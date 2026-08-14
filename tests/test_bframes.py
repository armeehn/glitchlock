"""Regression tests for B-frame backward motion vectors and no-op locks.

Both bugs here were found by running the tool over a corpus of carriers wider
than the one `prepare` produces. `prepare` never emits B-frames and never emits
an all-intra carrier, so neither case was reachable from the existing tests --
but both are reachable the moment a user brings their own MPEG-2 file, which
`lock` accepts.
"""

import json
import os
import subprocess

import pytest

from glitchlock import ffg
from glitchlock.core import NoOpLock, lock, select_features, unlock
from glitchlock.crypto import sha256_file
from glitchlock.domains import frame_slots

pytestmark = pytest.mark.skipif(not ffg.available(), reason="FFglitch not installed")

KEY = bytes(range(32))
NONCE = bytes(range(16))


def _source(path, size="256x192", duration=2, rate=25):
    subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size={size}:rate={rate}:duration={duration}",
         "-pix_fmt", "yuv420p", path],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="module")
def carrier_bframes(tmp_path_factory):
    """An MPEG-2 carrier that actually contains B-frames.

    ``ffg.transcode`` does not emit them, so this calls ffgac directly with
    ``-bf 2``. Real-world MPEG-2 -- DVD, broadcast, most camera output -- is
    full of B-frames, so this is the common case, not an exotic one.
    """
    d = tmp_path_factory.mktemp("bframes")
    src = str(d / "src.mp4")
    out = str(d / "carrier.mpg")
    _source(src)
    subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-y", "-i", src, "-an",
         "-mpv_flags", "+nopimb+forcemv", "-qscale:v", "6", "-g", "25",
         "-bf", "2", "-vcodec", "mpeg2video", "-f", "rawvideo", out],
        check=True, capture_output=True,
    )
    return out


@pytest.fixture(scope="module")
def carrier_all_intra(tmp_path_factory):
    """An MPEG-4 carrier with GOP 1, so it has no motion vectors at all."""
    d = tmp_path_factory.mktemp("allintra")
    src = str(d / "src.mp4")
    out = str(d / "carrier.m4v")
    _source(src)
    ffg.transcode(src, out, codec="mpeg4", gop=1)
    return out


def _mv_doc(path, tmp_path):
    return ffg.export(path, "mv", str(tmp_path / "mv.json"))


# ------------------------------------------------------------------ bcode


def test_bframe_carrier_actually_has_differing_bcode(carrier_bframes, tmp_path):
    """Guard the premise: if this stops holding, the tests below prove nothing."""
    doc = _mv_doc(carrier_bframes, tmp_path)
    frames = doc["streams"][0]["frames"]
    bframes = [f["mv"] for f in frames if f.get("mv") and "bcode" in f["mv"]]
    assert bframes, "fixture produced no B-frames"
    differing = [mv for mv in bframes if mv["fcode"] != mv["bcode"]]
    assert differing, "fixture produced no B-frame with bcode != fcode"


def test_backward_slots_use_bcode_not_fcode(carrier_bframes, tmp_path):
    """The domain of a backward vector comes from bcode.

    Before the fix, ``_mv_slots`` read ``fcode`` for both directions. Where
    bcode < fcode that silently permits values the codec cannot store (they
    wrap); where bcode > fcode it rejects a perfectly legal file.
    """
    doc = _mv_doc(carrier_bframes, tmp_path)
    stream = doc["streams"][0]
    codec = stream["codec"]
    checked = 0
    for frame in stream["frames"]:
        mv = frame.get("mv")
        if not mv or "bcode" not in mv or mv["fcode"] == mv["bcode"]:
            continue
        slots = frame_slots("mv", mv, codec)
        expected_back = 1 << (mv["bcode"][0] + 4)
        expected_fwd = 1 << (mv["fcode"][0] + 4)
        back = {n for _c, _k, path, (_lo, n) in slots if path[0] == "backward"}
        fwd = {n for _c, _k, path, (_lo, n) in slots if path[0] == "forward"}
        assert back == {expected_back}, (
            f"backward domain {back} should be {{{expected_back}}} "
            f"(bcode={mv['bcode']}), not the fcode-derived {expected_fwd}"
        )
        assert fwd == {expected_fwd}
        checked += 1
    assert checked, "no frame exercised the bcode path"


def test_bframe_carrier_locks_and_round_trips(carrier_bframes, tmp_path):
    """The whole point: a B-frame carrier must lock, and come back exact.

    Pre-fix this raised DomainViolation on the first B-frame whose bcode
    exceeded its fcode.
    """
    locked = str(tmp_path / "locked.bin")
    restored = str(tmp_path / "restored.bin")
    features = select_features(carrier_bframes, None)
    result = lock(carrier_bframes, locked, key=KEY, nonce=NONCE,
                  features=features, selftest=False)
    unlock(locked, restored, key=KEY, manifest=result.manifest)

    assert sha256_file(restored) == sha256_file(carrier_bframes)
    assert sha256_file(locked) != sha256_file(carrier_bframes)
    # A wrong domain shows up as repairs even when the round trip survives,
    # because the encoder wrapped what we asked it to write.
    assert sum(len(l.repairs) for l in result.manifest.layers) == 0


@pytest.mark.parametrize("mode", ["full", "substitute", "permute"])
def test_bframe_round_trip_all_modes(carrier_bframes, tmp_path, mode):
    locked = str(tmp_path / f"locked-{mode}.bin")
    restored = str(tmp_path / f"restored-{mode}.bin")
    features = select_features(carrier_bframes, None)
    result = lock(carrier_bframes, locked, key=KEY, nonce=NONCE,
                  features=features, mode=mode, selftest=False)
    unlock(locked, restored, key=KEY, manifest=result.manifest)
    assert sha256_file(restored) == sha256_file(carrier_bframes)


def test_p_frames_fall_back_to_fcode(carrier_bframes, tmp_path):
    """P-frames carry no bcode; forward vectors must still use fcode."""
    doc = _mv_doc(carrier_bframes, tmp_path)
    stream = doc["streams"][0]
    checked = 0
    for frame in stream["frames"]:
        mv = frame.get("mv")
        if not mv or "bcode" in mv or not mv.get("forward"):
            continue
        slots = frame_slots("mv", mv, stream["codec"])
        assert {n for _c, _k, _p, (_lo, n) in slots} == {1 << (mv["fcode"][0] + 4)}
        checked += 1
    assert checked, "no P-frame in fixture"


# ------------------------------------------------------------------ no-op


def test_all_intra_mpeg4_lock_is_refused(carrier_all_intra, tmp_path):
    """An all-intra MPEG-4 carrier has no motion vectors, so nothing scrambles.

    Pre-fix, ``lock`` wrote a byte-identical copy, printed 'self-test: pass'
    and exited 0 -- handing the user the plaintext labelled as ciphertext.
    """
    locked = str(tmp_path / "locked.bin")
    features = select_features(carrier_all_intra, None)
    with pytest.raises(NoOpLock):
        lock(carrier_all_intra, locked, key=KEY, nonce=NONCE, features=features)


def test_refused_lock_leaves_no_output_file(carrier_all_intra, tmp_path):
    """A refused lock must not leave a plaintext file at the output path.

    The layers run before the no-op check, so the output already exists by the
    time we refuse. Leaving it puts plaintext exactly where the user asked for
    ciphertext, under the name they chose for it.
    """
    locked = str(tmp_path / "locked.bin")
    features = select_features(carrier_all_intra, None)
    with pytest.raises(NoOpLock):
        lock(carrier_all_intra, locked, key=KEY, nonce=NONCE, features=features)
    assert not os.path.exists(locked), "refused lock left its output behind"


def test_all_intra_noop_can_be_forced(carrier_all_intra, tmp_path):
    """--allow-noop is the escape hatch, and it really does produce a copy."""
    locked = str(tmp_path / "locked.bin")
    features = select_features(carrier_all_intra, None)
    result = lock(carrier_all_intra, locked, key=KEY, nonce=NONCE,
                  features=features, allow_noop=True)
    assert sha256_file(locked) == sha256_file(carrier_all_intra)
    assert result.selftest_ok is True


def test_constant_qscale_permute_is_refused(tmp_path_factory, tmp_path):
    """Permuting values that are all equal is the identity map.

    A constant-qscale all-intra MPEG-2 carrier has no motion vectors, and its
    qscale slots all hold the same number, so --mode permute reports hundreds
    of slots 'scrambled' and changes nothing.
    """
    d = tmp_path_factory.mktemp("constq")
    src = str(d / "src.mp4")
    carrier = str(d / "carrier.mpg")
    _source(src)
    ffg.transcode(src, carrier, codec="mpeg2video", gop=1, qscale=6)

    locked = str(tmp_path / "locked.bin")
    features = select_features(carrier, None)
    with pytest.raises(NoOpLock) as excinfo:
        lock(carrier, locked, key=KEY, nonce=NONCE, features=features,
             mode="permute")
    # the message must not claim nothing was touched -- slots were touched,
    # the shuffle just had no effect
    assert "slots were" in str(excinfo.value)
    assert not os.path.exists(locked) or sha256_file(locked) == sha256_file(carrier)


# ------------------------------------------------------- verify determinism


def test_verify_is_reproducible(carrier_bframes, capsys):
    """Two `verify` runs on the same file must agree.

    They did not before: the nonce was drawn at random per run, so a carrier
    that round trips for most nonces and fails for a few (measured: interlaced
    MPEG-4, 7 of 12 nonces passing) reported whichever it happened to draw.
    """
    from glitchlock.cli import main

    rc1 = main(["verify", carrier_bframes])
    out1 = capsys.readouterr().out
    rc2 = main(["verify", carrier_bframes])
    out2 = capsys.readouterr().out
    assert rc1 == rc2 == 0
    assert out1 == out2


def test_verify_repeat_runs_every_nonce(carrier_bframes, capsys):
    from glitchlock.cli import main

    rc = main(["verify", carrier_bframes, "--repeat", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    for run in (1, 2, 3):
        assert f"run {run}/3:" in out
    assert "EXACT=3" in out


def test_verify_repeat_survives_a_failing_nonce(carrier_all_intra, capsys):
    """A run that raises must be recorded, not abort the sweep."""
    from glitchlock.cli import main

    # an all-intra carrier is a NO-OP for every nonce, so every run reports it
    rc = main(["verify", carrier_all_intra, "--repeat", "3"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("run ") >= 3
    assert "NO-OP=3" in out
