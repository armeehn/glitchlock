"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional, Tuple

from . import __version__, ffg, pubkey
from .core import LockError, lock, lockable_features, select_features, unlock
from .crypto import (
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    derive_key_from_password,
    key_from_env,
    load_key_file,
    random_nonce,
    random_salt,
    sha256_file,
)
from .domains import REJECTED_FEATURES, SUPPORTED_FEATURES
from .manifest import Manifest
from .transform import MODES


def _err(msg: str) -> None:
    print(f"glitchlock: {msg}", file=sys.stderr)


def _report(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------- keys


def _load_recipient(value: str) -> str:
    """A recipient may be given inline or as a path to a key file."""
    if value.startswith(pubkey.PUBLIC_PREFIX):
        return value.strip()
    if os.path.exists(value):
        return pubkey.read_key_file(value)
    raise LockError(
        f"recipient {value!r} is neither a '{pubkey.PUBLIC_PREFIX}' key nor an "
        "existing file"
    )


def _resolve_key_for_lock(args):
    """Return ``(key, kdf_params, keyless, seed_hex, kem)``."""
    recipients = getattr(args, "recipient", None)
    if recipients:
        keys = [_load_recipient(r) for r in recipients]
        content_key = pubkey.new_content_key()
        kem = pubkey.seal(content_key, keys)
        return content_key, {"algo": "x25519-kem"}, False, None, kem

    if args.keyless:
        seed = random_salt()
        return (
            derive_key_from_password(seed.hex(), b"glitchlock-keyless"),
            {"algo": "keyless", "note": "seed stored in manifest"},
            True,
            seed.hex(),
            None,
        )
    if args.key_file:
        return load_key_file(args.key_file), {"algo": "key-file-sha256"}, False, None, None
    env = key_from_env()
    if env is not None:
        return env, {"algo": "env-sha256"}, False, None, None
    password = args.password or getpass.getpass("passphrase: ")
    if not password:
        raise LockError("empty passphrase")
    salt = random_salt()
    kdf = {
        "algo": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": salt.hex(),
        "dklen": 32,
    }
    return derive_key_from_password(password, salt), kdf, False, None, None


def _resolve_key_for_unlock(args, manifest: Manifest) -> bytes:
    kdf = manifest.kdf or {}
    algo = kdf.get("algo")

    if manifest.kem:
        identity = getattr(args, "identity", None)
        if not identity:
            fps = pubkey.recipient_fingerprints(manifest.kem)
            raise LockError(
                "this file was locked to public keys; pass --identity with your "
                f"private key file. Recipients: {', '.join(fps) or 'none listed'}"
            )
        secret = (identity if identity.startswith(pubkey.SECRET_PREFIX)
                  else pubkey.read_key_file(identity))
        return pubkey.unseal(manifest.kem, secret)

    if manifest.keyless or algo == "keyless":
        if not manifest.seed:
            raise LockError("manifest claims to be keyless but carries no seed")
        return derive_key_from_password(manifest.seed, b"glitchlock-keyless")
    if args.key_file:
        return load_key_file(args.key_file)
    env = key_from_env()
    if env is not None and algo == "env-sha256":
        return env
    if algo == "key-file-sha256":
        if env is not None:
            return env
        raise LockError("this manifest was locked with a key file; pass --key-file")
    if algo == "scrypt":
        password = args.password or getpass.getpass("passphrase: ")
        return derive_key_from_password(password, bytes.fromhex(kdf["salt"]))
    raise LockError(f"unknown kdf {algo!r} in manifest")


# ----------------------------------------------------------------- commands


def cmd_inspect(args) -> int:
    codec = ffg.codec_name(args.input)
    print(f"ffglitch: {ffg.version()}")
    print(f"file:     {args.input}")
    print(f"codec:    {codec}")
    feats = ffg.supported_features(args.input)
    usable = lockable_features(args.input)
    print(f"sha256:   {sha256_file(args.input)}")
    print("features FFedit exposes:")
    for feature in feats:
        if feature in usable:
            tag = "reversible - glitchlock can use this"
        elif feature in SUPPORTED_FEATURES:
            tag = f"not verified reversible for codec {codec!r}"
        elif feature in REJECTED_FEATURES:
            tag = f"NOT reversible - {REJECTED_FEATURES[feature]}"
        else:
            tag = "unknown to glitchlock"
        print(f"  {feature:<14} {tag}")
    print()
    print(
        f"glitchlock would use: {', '.join(usable)}" if usable
        else "glitchlock cannot lock this file; run 'glitchlock prepare' first"
    )
    return 0 if usable else 1


def cmd_prepare(args) -> int:
    ffg.transcode(
        args.input, args.output, codec=args.codec, qscale=args.qscale,
        gop=args.gop, closed_gop=args.closed_gop,
    )
    feats = lockable_features(args.output)
    print(f"carrier: {args.output}")
    print(f"codec:   {args.codec}")
    print(f"sha256:  {sha256_file(args.output)}")
    print(f"lockable features: {', '.join(feats) or 'none'}")
    if not feats:
        _err("the produced carrier exposes no reversible feature")
        return 1
    return 0


def cmd_keygen(args) -> int:
    secret, public = pubkey.generate_identity()
    if args.output:
        pubkey.write_identity(args.output, secret, public)
        print(f"identity:    {args.output}  (mode 0600 - keep it secret)")
    else:
        print(secret)
    print(f"public key:  {public}")
    print(f"fingerprint: {pubkey.fingerprint(public)}")
    if args.output:
        print()
        print("Share the public key. Anyone holding it can lock a file for you;")
        print("only this identity file can unlock one.")
    return 0


def cmd_lock(args) -> int:
    features = select_features(args.input, args.features.split(",") if args.features else None)
    key, kdf, keyless, seed, kem = _resolve_key_for_lock(args)
    nonce = random_nonce()

    _report(f"locking {args.input}")
    _report(f"  features: {', '.join(features)}  mode: {args.mode}  intensity: {args.intensity}")

    result = lock(
        args.input,
        args.output,
        key=key,
        nonce=nonce,
        features=features,
        mode=args.mode,
        intensity=args.intensity,
        selftest=not args.no_selftest,
        report=_report,
        segment=args.segment,
    )
    manifest = result.manifest
    manifest.kdf = kdf
    manifest.keyless = keyless
    manifest.seed = seed
    manifest.kem = kem
    manifest.sign(key)
    manifest.save(args.manifest)

    print(f"locked:   {args.output}")
    print(f"manifest: {args.manifest}")
    print(f"self-test: {manifest.selftest}")
    if kem:
        fps = pubkey.recipient_fingerprints(kem)
        print(f"recipients: {len(fps)} ({', '.join(fps)})")
        print("only a matching private key can unlock this")
    if keyless:
        print("mode: keyless - the manifest alone can unwind this file")
    total_repairs = sum(len(l.repairs) for l in manifest.layers)
    if total_repairs:
        print(f"repairs recorded in manifest: {total_repairs}")
    return 0


def cmd_unlock(args) -> int:
    manifest = Manifest.load(args.manifest)
    key = _resolve_key_for_unlock(args, manifest)

    if not manifest.verify(key):
        _err(
            "manifest MAC check failed: either the key/passphrase is wrong or the "
            "manifest has been modified. Refusing to continue."
        )
        return 2

    _report(f"unlocking {args.input}")
    unlock(args.input, args.output, key=key, manifest=manifest, report=_report)

    digest = sha256_file(args.output)
    print(f"restored: {args.output}")
    if manifest.carrier_sha256:
        if digest == manifest.carrier_sha256:
            print("integrity: OK - byte-exact match with the original carrier")
            return 0
        print("integrity: MISMATCH")
        print(f"  expected {manifest.carrier_sha256}")
        print(f"  got      {digest}")
        return 3
    print(f"sha256: {digest}")
    return 0


def cmd_verify(args) -> int:
    """Lock and unlock a file in a scratch directory and report the outcome."""
    import shutil
    import tempfile

    features = select_features(args.input, args.features.split(",") if args.features else None)
    workdir = tempfile.mkdtemp(prefix="glitchlock-verify-")
    try:
        locked_path = os.path.join(workdir, "locked.bin")
        restored_path = os.path.join(workdir, "restored.bin")
        key = load_key_file(args.key_file) if args.key_file else b"\x00" * 32
        nonce = random_nonce()

        result = lock(
            args.input, locked_path, key=key, nonce=nonce, features=features,
            mode=args.mode, intensity=args.intensity, selftest=False, report=_report,
        )
        unlock(locked_path, restored_path, key=key, manifest=result.manifest,
               report=_report)

        original = sha256_file(args.input)
        restored = sha256_file(restored_path)
        changed = sha256_file(locked_path) != original
        repairs = sum(len(l.repairs) for l in result.manifest.layers)
        touched = sum(l.slots_touched for l in result.manifest.layers)

        print(f"features:   {', '.join(features)}")
        print(f"slots kept:  {touched} scrambled")
        print(f"repairs:     {repairs}")
        print(f"ciphertext differs from plaintext: {'yes' if changed else 'NO (suspicious)'}")
        print(f"round trip:  {'EXACT' if original == restored else 'MISMATCH'}")
        return 0 if (original == restored and changed) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glitchlock",
        description="Reversible, keyed bitstream scrambling for video, built on FFglitch.",
    )
    parser.add_argument("--version", action="version", version=f"glitchlock {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="show what can be locked in a file")
    p.add_argument("input")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("prepare", help="transcode any video into a glitchable carrier")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--codec", default="mpeg2video", choices=["mpeg2video", "mpeg4"])
    p.add_argument("--qscale", type=int, default=6)
    p.add_argument("--gop", type=int, default=25)
    p.add_argument("--closed-gop", action="store_true",
                   help="self-contained GOPs, so the carrier can be split into "
                        "independently lockable stream segments")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("keygen", help="generate an X25519 identity for recipient mode")
    p.add_argument("-o", "--output", help="write the private identity here (mode 0600)")
    p.set_defaults(func=cmd_keygen)

    def add_key_args(sp):
        sp.add_argument("--key-file", help="file whose contents are hashed into the key")
        sp.add_argument("--password", help="passphrase (prompted if omitted)")

    p = sub.add_parser("lock", help="scramble a carrier and write a manifest")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("-m", "--manifest", required=True)
    p.add_argument("--features", help=f"comma separated; default: all of {','.join(SUPPORTED_FEATURES)} present")
    p.add_argument("--mode", default="full", choices=list(MODES))
    p.add_argument("--intensity", type=float, default=1.0,
                   help="fraction of slots to scramble, 0 < i <= 1 (default 1.0)")
    p.add_argument("--segment", type=int, default=0,
                   help="streaming segment number; give each piece of a chunked "
                        "stream its own so they do not share a keystream")
    p.add_argument("--keyless", action="store_true",
                   help="store the seed in the manifest; manifest alone can unwind")
    p.add_argument("--no-selftest", action="store_true",
                   help="skip proving reversibility before writing the manifest")
    p.add_argument("--recipient", action="append", metavar="PUBKEY",
                   help="public key or key file; repeat for several recipients. "
                        "Overrides passphrase and key-file modes.")
    add_key_args(p)
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("unlock", help="restore a locked file using its manifest")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("-m", "--manifest", required=True)
    p.add_argument("--identity", metavar="FILE",
                   help="private identity file, for a file locked to public keys")
    add_key_args(p)
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("verify", help="lock+unlock in a scratch dir and report exactness")
    p.add_argument("input")
    p.add_argument("--features")
    p.add_argument("--mode", default="full", choices=list(MODES))
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument("--key-file")
    p.set_defaults(func=cmd_verify)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ffg.FFglitchMissing as exc:
        _err(str(exc))
        return 4
    except pubkey.NotARecipient as exc:
        _err(str(exc))
        return 2
    except (LockError, ffg.FFglitchError, pubkey.PubKeyError, ValueError) as exc:
        _err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
