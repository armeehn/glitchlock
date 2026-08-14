"""Tests for the keystream, KDF and manifest signing."""

import collections

import pytest

from glitchlock.crypto import (
    KeyStream,
    derive_key_from_password,
    mac_bytes,
    random_nonce,
)
from glitchlock.manifest import Layer, Manifest

KEY = bytes(range(32))
NONCE = bytes(range(16))


def test_keystream_is_deterministic():
    a = KeyStream(KEY, NONCE, b"label").read(256)
    b = KeyStream(KEY, NONCE, b"label").read(256)
    assert a == b


def test_keystream_domain_separation():
    a = KeyStream(KEY, NONCE, b"mv|0|0").read(64)
    b = KeyStream(KEY, NONCE, b"mv|0|1").read(64)
    c = KeyStream(KEY, random_nonce(), b"mv|0|0").read(64)
    assert a != b and a != c


def test_read_is_chunk_size_independent():
    whole = KeyStream(KEY, NONCE, b"x").read(200)
    stream = KeyStream(KEY, NONCE, b"x")
    pieces = b"".join(stream.read(n) for n in (1, 7, 32, 60, 100))
    assert pieces == whole


def test_randbelow_range_and_spread():
    ks = KeyStream(KEY, NONCE, b"spread")
    counts = collections.Counter(ks.randbelow(7) for _ in range(7000))
    assert set(counts) <= set(range(7))
    # every bucket should be hit; a biased or broken sampler would not manage it
    assert len(counts) == 7
    assert min(counts.values()) > 700


def test_randbelow_edge_cases():
    ks = KeyStream(KEY, NONCE, b"edge")
    assert ks.randbelow(1) == 0
    assert ks.randbelow(0) == 0


def test_permutation_is_a_permutation():
    ks = KeyStream(KEY, NONCE, b"perm")
    for size in (0, 1, 2, 17, 256):
        perm = ks.permutation(size)
        assert sorted(perm) == list(range(size))


def test_scrypt_is_salt_dependent():
    a = derive_key_from_password("hunter2", b"a" * 16)
    b = derive_key_from_password("hunter2", b"b" * 16)
    assert a != b and len(a) == 32


def test_manifest_sign_and_verify():
    manifest = Manifest(nonce=NONCE.hex(), carrier_sha256="ab" * 32)
    manifest.layers.append(Layer(feature="mv", mode="full", intensity=1.0))
    manifest.sign(KEY)
    assert manifest.verify(KEY)
    assert not manifest.verify(bytes(32))


def test_manifest_tamper_detected():
    manifest = Manifest(nonce=NONCE.hex())
    manifest.layers.append(Layer(feature="mv", mode="full", intensity=1.0))
    manifest.sign(KEY)
    manifest.layers[0].intensity = 0.5
    assert not manifest.verify(KEY)


def test_manifest_roundtrip_file(tmp_path):
    manifest = Manifest(nonce=NONCE.hex(), codec="mpeg2video")
    manifest.layers.append(Layer(feature="mv", mode="full", intensity=1.0,
                                 repairs={"0/1/forward/0/0/0": 5}))
    manifest.sign(KEY)
    path = tmp_path / "m.json"
    manifest.save(str(path))
    loaded = Manifest.load(str(path))
    assert loaded.verify(KEY)
    assert loaded.layers[0].repairs == {"0/1/forward/0/0/0": 5}


def test_manifest_rejects_foreign_file(tmp_path):
    path = tmp_path / "nope.json"
    path.write_text('{"format": "something-else"}')
    with pytest.raises(ValueError):
        Manifest.load(str(path))


def test_mac_differs_by_key():
    assert mac_bytes(KEY, b"x") != mac_bytes(bytes(32), b"x")
