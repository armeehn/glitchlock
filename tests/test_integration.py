"""End-to-end tests against real FFglitch and real bitstreams.

Skipped automatically when FFglitch is not installed, so `pytest` still passes
on a bare checkout. Install FFglitch and re-run to exercise the real thing.
"""

import os
import subprocess

import pytest

from glitchlock import ffg
from glitchlock.core import lock, select_features, unlock
from glitchlock.crypto import sha256_file
from glitchlock.manifest import Manifest

pytestmark = pytest.mark.skipif(not ffg.available(), reason="FFglitch not installed")

KEY = bytes(range(32))
NONCE = bytes(range(16))


def _make_source(path, size="256x192", duration=2):
    """Generate a synthetic clip using ffgac's own lavfi support."""
    subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size={size}:rate=25:duration={duration}",
         "-pix_fmt", "yuv420p", path],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="module")
def carrier_mpeg2(tmp_path_factory):
    d = tmp_path_factory.mktemp("carrier2")
    src = str(d / "src.mp4")
    out = str(d / "carrier.mpg")
    _make_source(src)
    ffg.transcode(src, out, codec="mpeg2video")
    return out


@pytest.fixture(scope="module")
def carrier_mpeg4(tmp_path_factory):
    d = tmp_path_factory.mktemp("carrier4")
    src = str(d / "src.mp4")
    out = str(d / "carrier.m4v")
    _make_source(src)
    ffg.transcode(src, out, codec="mpeg4")
    return out


def _roundtrip(carrier, tmp_path, features=None, mode="full", intensity=1.0):
    locked = str(tmp_path / "locked.bin")
    restored = str(tmp_path / "restored.bin")
    chosen = select_features(carrier, features)
    result = lock(carrier, locked, key=KEY, nonce=NONCE, features=chosen,
                  mode=mode, intensity=intensity, selftest=False)
    unlock(locked, restored, key=KEY, manifest=result.manifest)
    return result, locked, restored


def test_mpeg2_roundtrip_is_byte_exact(carrier_mpeg2, tmp_path):
    result, locked, restored = _roundtrip(carrier_mpeg2, tmp_path)
    assert sha256_file(locked) != sha256_file(carrier_mpeg2), "locking changed nothing"
    assert sha256_file(restored) == sha256_file(carrier_mpeg2)


def test_mpeg4_roundtrip_is_byte_exact(carrier_mpeg4, tmp_path):
    result, locked, restored = _roundtrip(carrier_mpeg4, tmp_path)
    assert sha256_file(locked) != sha256_file(carrier_mpeg4)
    assert sha256_file(restored) == sha256_file(carrier_mpeg4)


@pytest.mark.parametrize("mode", ["full", "permute", "substitute"])
def test_all_modes_roundtrip(carrier_mpeg2, tmp_path, mode):
    _r, locked, restored = _roundtrip(carrier_mpeg2, tmp_path, mode=mode)
    assert sha256_file(locked) != sha256_file(carrier_mpeg2)
    assert sha256_file(restored) == sha256_file(carrier_mpeg2)


@pytest.mark.parametrize("intensity", [0.05, 0.5])
def test_partial_intensity_roundtrip(carrier_mpeg2, tmp_path, intensity):
    _r, locked, restored = _roundtrip(carrier_mpeg2, tmp_path, intensity=intensity)
    assert sha256_file(locked) != sha256_file(carrier_mpeg2)
    assert sha256_file(restored) == sha256_file(carrier_mpeg2)


def test_single_feature_layers(carrier_mpeg2, tmp_path):
    for feature in (["mv"], ["qscale"]):
        sub = tmp_path / feature[0]
        sub.mkdir()
        _r, locked, restored = _roundtrip(carrier_mpeg2, sub, features=feature)
        assert sha256_file(locked) != sha256_file(carrier_mpeg2)
        assert sha256_file(restored) == sha256_file(carrier_mpeg2)


def test_no_repairs_needed_on_supported_features(carrier_mpeg2, tmp_path):
    """The repair list exists as a safety net; on mv/qscale it should stay empty."""
    result, _locked, _restored = _roundtrip(carrier_mpeg2, tmp_path)
    for layer in result.manifest.layers:
        assert layer.repairs == {}, f"{layer.feature} needed repairs: {layer.repairs}"


def test_wrong_key_fails_to_restore(carrier_mpeg2, tmp_path):
    locked = str(tmp_path / "locked.bin")
    restored = str(tmp_path / "restored.bin")
    chosen = select_features(carrier_mpeg2, None)
    result = lock(carrier_mpeg2, locked, key=KEY, nonce=NONCE, features=chosen,
                  selftest=False)
    unlock(locked, restored, key=bytes(32), manifest=result.manifest,
           verify_input=False)
    assert sha256_file(restored) != sha256_file(carrier_mpeg2)


def test_locked_file_still_decodes(carrier_mpeg2, tmp_path):
    """The ciphertext must remain a playable video, not corrupt garbage."""
    _r, locked, _restored = _roundtrip(carrier_mpeg2, tmp_path)
    proc = subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-i", locked, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_selftest_and_manifest_flow(carrier_mpeg2, tmp_path):
    locked = str(tmp_path / "locked.bin")
    manifest_path = str(tmp_path / "manifest.json")
    restored = str(tmp_path / "restored.bin")

    result = lock(carrier_mpeg2, locked, key=KEY, nonce=NONCE,
                  features=select_features(carrier_mpeg2, None), selftest=True)
    assert result.selftest_ok is True
    result.manifest.sign(KEY)
    result.manifest.save(manifest_path)

    loaded = Manifest.load(manifest_path)
    assert loaded.verify(KEY)
    unlock(locked, restored, key=KEY, manifest=loaded)
    assert sha256_file(restored) == loaded.carrier_sha256


def test_irreversible_feature_is_refused(carrier_mpeg2):
    from glitchlock.core import LockError

    with pytest.raises(LockError):
        select_features(carrier_mpeg2, ["q_dc"])


def test_cli_end_to_end(carrier_mpeg2, tmp_path):
    import sys
    from glitchlock.cli import main

    keyfile = tmp_path / "key.bin"
    keyfile.write_bytes(os.urandom(64))
    locked = str(tmp_path / "locked.mpg")
    manifest = str(tmp_path / "m.json")
    restored = str(tmp_path / "restored.mpg")

    assert main(["lock", carrier_mpeg2, "-o", locked, "-m", manifest,
                 "--key-file", str(keyfile)]) == 0
    assert main(["unlock", locked, "-o", restored, "-m", manifest,
                 "--key-file", str(keyfile)]) == 0
    assert sha256_file(restored) == sha256_file(carrier_mpeg2)

    # a wrong key must be rejected by the manifest MAC, not silently mangle
    wrong = tmp_path / "wrong.bin"
    wrong.write_bytes(os.urandom(64))
    assert main(["unlock", locked, "-o", restored, "-m", manifest,
                 "--key-file", str(wrong)]) == 2


def test_cli_verify_command(carrier_mpeg2):
    from glitchlock.cli import main

    assert main(["verify", carrier_mpeg2]) == 0


def test_keyless_flow(carrier_mpeg2, tmp_path):
    from glitchlock.cli import main

    locked = str(tmp_path / "k.mpg")
    manifest = str(tmp_path / "k.json")
    restored = str(tmp_path / "kr.mpg")
    assert main(["lock", carrier_mpeg2, "-o", locked, "-m", manifest, "--keyless"]) == 0
    assert main(["unlock", locked, "-o", restored, "-m", manifest]) == 0
    assert sha256_file(restored) == sha256_file(carrier_mpeg2)
