"""Public-key recipients: key handling, sealing, and the end-to-end flow."""

import os

import pytest

from glitchlock import ffg, pubkey
from glitchlock.crypto import sha256_file

crypto_available = True
try:
    pubkey.generate_identity()
except pubkey.MissingDependency:  # pragma: no cover
    crypto_available = False

pytestmark = pytest.mark.skipif(
    not crypto_available, reason="cryptography not installed")


# ------------------------------------------------------------- key handling


def test_identity_roundtrip():
    secret, public = pubkey.generate_identity()
    assert secret.startswith(pubkey.SECRET_PREFIX)
    assert public.startswith(pubkey.PUBLIC_PREFIX)
    assert pubkey.public_from_secret(secret) == public


def test_identities_are_distinct():
    a, _ = pubkey.generate_identity()
    b, _ = pubkey.generate_identity()
    assert a != b


def test_fingerprint_is_stable_and_key_specific():
    _s1, p1 = pubkey.generate_identity()
    _s2, p2 = pubkey.generate_identity()
    assert pubkey.fingerprint(p1) == pubkey.fingerprint(p1)
    assert pubkey.fingerprint(p1) != pubkey.fingerprint(p2)
    assert len(pubkey.fingerprint(p1)) == 16


def test_malformed_keys_are_rejected():
    with pytest.raises(pubkey.PubKeyError):
        pubkey.fingerprint("not-a-key")
    with pytest.raises(pubkey.PubKeyError):
        pubkey.public_from_secret("glk-sec-v1:####")
    with pytest.raises(pubkey.PubKeyError):
        pubkey.public_from_secret(pubkey.SECRET_PREFIX + "c2hvcnQ=")  # wrong length


def test_secret_and_public_prefixes_are_not_interchangeable():
    secret, public = pubkey.generate_identity()
    with pytest.raises(pubkey.PubKeyError):
        pubkey.fingerprint(secret)
    with pytest.raises(pubkey.PubKeyError):
        pubkey.public_from_secret(public)


def test_identity_file_is_private_and_readable_back(tmp_path):
    secret, public = pubkey.generate_identity()
    path = str(tmp_path / "id.key")
    pubkey.write_identity(path, secret, public)
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    assert pubkey.read_key_file(path) == secret


def test_key_file_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "k.txt"
    _s, public = pubkey.generate_identity()
    path.write_text(f"# a comment\n\n{public}\n")
    assert pubkey.read_key_file(str(path)) == public


# ------------------------------------------------------------------ sealing


def test_seal_and_unseal():
    secret, public = pubkey.generate_identity()
    ck = pubkey.new_content_key()
    kem = pubkey.seal(ck, [public])
    assert pubkey.unseal(kem, secret) == ck


def test_content_key_is_not_in_the_manifest_block():
    secret, public = pubkey.generate_identity()
    ck = pubkey.new_content_key()
    kem = pubkey.seal(ck, [public])
    blob = repr(kem).encode()
    assert ck not in blob
    assert ck.hex().encode() not in blob


def test_wrong_identity_cannot_unseal():
    _s1, public = pubkey.generate_identity()
    other, _p2 = pubkey.generate_identity()
    kem = pubkey.seal(pubkey.new_content_key(), [public])
    with pytest.raises(pubkey.NotARecipient):
        pubkey.unseal(kem, other)


def test_multiple_recipients_each_open_the_same_key():
    ids = [pubkey.generate_identity() for _ in range(3)]
    ck = pubkey.new_content_key()
    kem = pubkey.seal(ck, [p for _s, p in ids])
    assert len(kem["recipients"]) == 3
    for secret, _public in ids:
        assert pubkey.unseal(kem, secret) == ck


def test_duplicate_recipients_collapse():
    _s, public = pubkey.generate_identity()
    kem = pubkey.seal(pubkey.new_content_key(), [public, public, public])
    assert len(kem["recipients"]) == 1


def test_sealing_twice_produces_unrelated_wrappings():
    """Fresh ephemeral key per lock: no reuse across files."""
    _s, public = pubkey.generate_identity()
    ck = pubkey.new_content_key()
    a = pubkey.seal(ck, [public])
    b = pubkey.seal(ck, [public])
    assert a["recipients"][0]["epk"] != b["recipients"][0]["epk"]
    assert a["recipients"][0]["wrapped"] != b["recipients"][0]["wrapped"]


def test_tampered_wrapping_is_rejected_not_silently_wrong():
    secret, public = pubkey.generate_identity()
    kem = pubkey.seal(pubkey.new_content_key(), [public])
    entry = kem["recipients"][0]
    raw = bytearray(pubkey._unb64(entry["wrapped"]))
    raw[-1] ^= 0x01
    entry["wrapped"] = pubkey._b64(bytes(raw))
    with pytest.raises(pubkey.NotARecipient):
        pubkey.unseal(kem, secret)


def test_a_forged_fingerprint_grants_nothing():
    """The fingerprint is a lookup hint, never the thing that authorises."""
    _s1, public = pubkey.generate_identity()
    attacker, attacker_pub = pubkey.generate_identity()
    kem = pubkey.seal(pubkey.new_content_key(), [public])
    kem["recipients"][0]["fp"] = pubkey.fingerprint(attacker_pub)
    with pytest.raises(pubkey.NotARecipient):
        pubkey.unseal(kem, attacker)


def test_empty_recipient_list_is_refused():
    with pytest.raises(pubkey.PubKeyError):
        pubkey.seal(pubkey.new_content_key(), [])


def test_unknown_algorithm_is_refused():
    secret, public = pubkey.generate_identity()
    kem = pubkey.seal(pubkey.new_content_key(), [public])
    kem["algo"] = "rot13"
    with pytest.raises(pubkey.PubKeyError):
        pubkey.unseal(kem, secret)


# --------------------------------------------------------------- end to end


needs_ffglitch = pytest.mark.skipif(
    not ffg.available(), reason="FFglitch not installed")


@pytest.fixture(scope="module")
def carrier(tmp_path_factory):
    d = tmp_path_factory.mktemp("pk")
    src, out = str(d / "s.mp4"), str(d / "carrier.mpg")
    import subprocess
    subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=192x144:rate=25:duration=2",
         "-pix_fmt", "yuv420p", src], check=True, capture_output=True)
    ffg.transcode(src, out, codec="mpeg2video")
    return out


@needs_ffglitch
def test_cli_recipient_roundtrip(carrier, tmp_path):
    from glitchlock.cli import main

    identity = str(tmp_path / "me.key")
    assert main(["keygen", "-o", identity]) == 0
    public = pubkey.public_from_secret(pubkey.read_key_file(identity))

    locked = str(tmp_path / "locked.mpg")
    manifest = str(tmp_path / "m.json")
    restored = str(tmp_path / "restored.mpg")

    assert main(["lock", carrier, "-o", locked, "-m", manifest,
                 "--recipient", public]) == 0
    assert main(["unlock", locked, "-o", restored, "-m", manifest,
                 "--identity", identity]) == 0
    assert sha256_file(restored) == sha256_file(carrier)


@needs_ffglitch
def test_cli_wrong_identity_is_refused(carrier, tmp_path):
    from glitchlock.cli import main

    mine = str(tmp_path / "a.key")
    theirs = str(tmp_path / "b.key")
    main(["keygen", "-o", mine])
    main(["keygen", "-o", theirs])
    public = pubkey.public_from_secret(pubkey.read_key_file(mine))

    locked = str(tmp_path / "l.mpg")
    manifest = str(tmp_path / "m.json")
    assert main(["lock", carrier, "-o", locked, "-m", manifest,
                 "--recipient", public]) == 0
    assert main(["unlock", locked, "-o", str(tmp_path / "x.mpg"),
                 "-m", manifest, "--identity", theirs]) == 2


@needs_ffglitch
def test_cli_recipient_from_key_file(carrier, tmp_path):
    from glitchlock.cli import main

    identity = str(tmp_path / "id.key")
    main(["keygen", "-o", identity])
    public = pubkey.public_from_secret(pubkey.read_key_file(identity))
    pub_path = tmp_path / "id.pub"
    pub_path.write_text("# my public key\n" + public + "\n")

    locked = str(tmp_path / "l.mpg")
    manifest = str(tmp_path / "m.json")
    restored = str(tmp_path / "r.mpg")
    assert main(["lock", carrier, "-o", locked, "-m", manifest,
                 "--recipient", str(pub_path)]) == 0
    assert main(["unlock", locked, "-o", restored, "-m", manifest,
                 "--identity", identity]) == 0
    assert sha256_file(restored) == sha256_file(carrier)


@needs_ffglitch
def test_cli_unlock_without_identity_explains_itself(carrier, tmp_path, capsys):
    from glitchlock.cli import main

    identity = str(tmp_path / "id.key")
    main(["keygen", "-o", identity])
    public = pubkey.public_from_secret(pubkey.read_key_file(identity))
    locked = str(tmp_path / "l.mpg")
    manifest = str(tmp_path / "m.json")
    main(["lock", carrier, "-o", locked, "-m", manifest, "--recipient", public])

    assert main(["unlock", locked, "-o", str(tmp_path / "x.mpg"),
                 "-m", manifest]) == 1
    assert "--identity" in capsys.readouterr().err


@needs_ffglitch
def test_manifest_holds_no_usable_secret(carrier, tmp_path):
    """The manifest travels with the video; it must not be enough on its own."""
    from glitchlock.cli import main
    from glitchlock.manifest import Manifest

    identity = str(tmp_path / "id.key")
    main(["keygen", "-o", identity])
    public = pubkey.public_from_secret(pubkey.read_key_file(identity))
    locked = str(tmp_path / "l.mpg")
    manifest_path = str(tmp_path / "m.json")
    main(["lock", carrier, "-o", locked, "-m", manifest_path, "--recipient", public])

    m = Manifest.load(manifest_path)
    assert m.kem and m.kem["recipients"]
    assert m.seed is None
    assert not m.keyless
    # an unrelated identity gets nothing out of it
    other, _p = pubkey.generate_identity()
    with pytest.raises(pubkey.NotARecipient):
        pubkey.unseal(m.kem, other)


@needs_ffglitch
def test_session_key_sealed_once_covers_a_whole_stream(tmp_path):
    """The shape a live broadcast should use.

    One asymmetric handshake per session, not per segment: seal a random session
    key to the subscriber's public key once, then lock every segment under it
    with a distinct segment number. The subscriber needs the handshake blob once
    and can then unlock every segment from a counter alone.
    """
    import subprocess

    from glitchlock.core import lock, select_features, unlock
    from glitchlock.crypto import random_nonce
    from glitchlock.manifest import Layer, Manifest
    from glitchlock.stream import split_segments

    src, car = str(tmp_path / "s.mp4"), str(tmp_path / "c.mpg")
    subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=192x144:rate=25:duration=3",
         "-pix_fmt", "yuv420p", src], check=True, capture_output=True)
    ffg.transcode(src, car, codec="mpeg2video", gop=12, closed_gop=True)

    subscriber_secret, subscriber_public = pubkey.generate_identity()

    # session handshake, once
    session_key = pubkey.new_content_key()
    kem = pubkey.seal(session_key, [subscriber_public])
    nonce = random_nonce()
    assert pubkey.unseal(kem, subscriber_secret) == session_key

    segments = split_segments(open(car, "rb").read())
    assert len(segments) > 2
    features = select_features(car, None)

    locked = []
    for n, seg in enumerate(segments):
        a, b = tmp_path / f"i{n}.mpg", tmp_path / f"o{n}.mpg"
        a.write_bytes(seg)
        lock(str(a), str(b), key=session_key, nonce=nonce, features=features,
             selftest=False, segment=n + 1)
        locked.append(b.read_bytes())

    # subscriber side: session key from the handshake, segment number from position
    recovered_key = pubkey.unseal(kem, subscriber_secret)
    for n, blob in enumerate(locked):
        a, b = tmp_path / f"r{n}.mpg", tmp_path / f"u{n}.mpg"
        a.write_bytes(blob)
        synth = Manifest(nonce=nonce.hex(), segment=n + 1)
        for f in features:
            synth.layers.append(Layer(feature=f, mode="full", intensity=1.0))
        unlock(str(a), str(b), key=recovered_key, manifest=synth,
               verify_input=False)
        assert b.read_bytes() == segments[n], f"segment {n} did not recover"

    # reusing one session key across segments is only safe because the segment
    # numbers differ, so identical input must still lock differently
    same = tmp_path / "same.mpg"
    same.write_bytes(segments[1])
    outs = []
    for seg_no in (1, 2):
        d = tmp_path / f"same{seg_no}.mpg"
        lock(str(same), str(d), key=session_key, nonce=nonce, features=features,
             selftest=False, segment=seg_no)
        outs.append(d.read_bytes())
    assert outs[0] != outs[1]


@needs_ffglitch
def test_two_recipients_both_recover_the_same_file(carrier, tmp_path):
    from glitchlock.cli import main

    ids, pubs = [], []
    for name in ("alice", "bob"):
        p = str(tmp_path / f"{name}.key")
        main(["keygen", "-o", p])
        ids.append(p)
        pubs.append(pubkey.public_from_secret(pubkey.read_key_file(p)))

    locked = str(tmp_path / "l.mpg")
    manifest = str(tmp_path / "m.json")
    assert main(["lock", carrier, "-o", locked, "-m", manifest,
                 "--recipient", pubs[0], "--recipient", pubs[1]]) == 0

    for n, identity in enumerate(ids):
        out = str(tmp_path / f"r{n}.mpg")
        assert main(["unlock", locked, "-o", out, "-m", manifest,
                     "--identity", identity]) == 0
        assert sha256_file(out) == sha256_file(carrier)
