"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional, Tuple

from . import __version__, ffg, mp2, pubkey
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
from .stream import StreamSession, run_stream
from .transport import AudioSpec, TransportSession, demux, run_transport
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


def _inspect_audio(args) -> int:
    with open(args.input, "rb") as fh:
        info = mp2.describe(fh.read())
    print(f"file:     {args.input}")
    print(f"codec:    {mp2.CODEC_NAME} ({info.version} Audio Layer II)")
    print(f"stream:   {info.samplerate} Hz, {info.mode}, {info.bitrate_kbps} kbps, "
          f"{info.frames} frames ({info.seconds:.1f} s)")
    print(f"crc:      {info.protected}/{info.frames} frames protected")
    if info.junk_bytes:
        print(f"junk:     {info.junk_bytes} bytes outside any frame (carried verbatim)")
    print(f"sha256:   {sha256_file(args.input)}")
    print()
    print(f"lockable features: {', '.join(mp2.FEATURES)}")
    return 0


def cmd_inspect(args) -> int:
    layer = mp2.sniff_file(args.input)
    if layer == mp2.LAYER_NAMES[mp2.LAYER_II]:
        return _inspect_audio(args)
    if layer is not None:
        _err(f"MPEG audio {layer} is not supported; only Layer II (MP2) is")
        return 1
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
        gop=args.gop, closed_gop=args.closed_gop, container=args.container,
    )
    print(f"carrier: {args.output}")
    print(f"codec:   {args.codec}")
    print(f"sha256:  {sha256_file(args.output)}")
    if args.container == "ts":
        # FFedit reads elementary streams only; report the tracks instead
        with open(args.output, "rb") as fh:
            program = demux(fh.read())
        print("tracks:  " + ", ".join(f"{t.codec} (pid {t.pid:#x})" for t in program.tracks))
        return 0
    feats = lockable_features(args.output)
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
        allow_noop=args.allow_noop,
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


# ------------------------------------------------------------------ streaming


def _open_stream(path: Optional[str], mode: str):
    """``-`` or nothing means the process's own stdin/stdout, in binary."""
    if not path or path == "-":
        return sys.stdin.buffer if "r" in mode else sys.stdout.buffer
    return open(path, mode)


def cmd_stream_lock(args) -> int:
    features = args.features.split(",") if args.features else ["mv"]
    key, kdf, keyless, _seed, kem = _resolve_key_for_lock(args)
    if keyless or kem:
        _err("stream mode takes a key file or passphrase only")
        return 2

    session = StreamSession(
        codec="", nonce=random_nonce().hex(), features=features,
        mode=args.mode, intensity=args.intensity, gops=args.gops, kdf=kdf,
    )
    with _open_stream(args.input, "rb") as src, _open_stream(args.output, "wb") as dst:
        stats = run_stream(src, dst, session, key, forward=True,
                           workers=args.workers, report=_report if args.verbose else _noop)
    session.save(args.session)

    _report(f"locked {stats.segments} segments, {stats.frames} frames, "
            f"{stats.slots_touched} slots, {stats.bytes_in} -> {stats.bytes_out} bytes")
    _report(f"session: {args.session}  (needed by the receiver, holds no key)")
    return 0


def cmd_stream_unlock(args) -> int:
    session = StreamSession.load(args.session)
    key = _resolve_key_for_unlock(args, session.manifest_for(session.first_segment))

    with _open_stream(args.input, "rb") as src, _open_stream(args.output, "wb") as dst:
        stats = run_stream(src, dst, session, key, forward=False,
                           workers=args.workers, report=_report if args.verbose else _noop)
    _report(f"unlocked {stats.segments} segments, {stats.bytes_in} -> {stats.bytes_out} bytes")
    return 0


def cmd_ts_lock(args) -> int:
    key, kdf, keyless, _seed, kem = _resolve_key_for_lock(args)
    if keyless or kem:
        _err("transport mode takes a key file or passphrase only")
        return 2
    video = StreamSession(
        codec="", nonce=random_nonce().hex(), features=args.features.split(","),
        mode=args.mode, intensity=args.intensity, gops=args.gops, kdf=kdf,
    )
    audio = None
    if args.audio_features != "none":
        audio = AudioSpec(features=args.audio_features.split(","), mode=args.mode,
                          intensity=args.intensity)
    session = TransportSession(video=video, audio=audio)

    with open(args.input, "rb") as fh:
        out, stats = run_transport(fh.read(), session, key, forward=True,
                                   workers=args.workers)
    with open(args.output, "wb") as fh:
        fh.write(out)
    session.save(args.session)

    _report(f"locked {stats.video_segments} video segments, {stats.frames} frames, "
            f"{stats.slots_touched} slots; {stats.audio_frames} audio frames")
    _report(f"session: {args.session}  (needed by the receiver, holds no key)")
    return 0


def cmd_ts_unlock(args) -> int:
    session = TransportSession.load(args.session)
    key = _resolve_key_for_unlock(args, session.video.manifest_for(session.video.first_segment))

    with open(args.input, "rb") as fh:
        out, stats = run_transport(fh.read(), session, key, forward=False,
                                   workers=args.workers)
    with open(args.output, "wb") as fh:
        fh.write(out)
    _report(f"unlocked {stats.video_segments} video segments, {stats.audio_frames} audio frames")
    return 0


def _noop(_msg: str) -> None:
    pass


def _add_stream_io(p) -> None:
    p.add_argument("-i", "--input", default="-", help="elementary stream, or - for stdin")
    p.add_argument("-o", "--output", default="-", help="destination, or - for stdout")
    p.add_argument("--workers", type=int, default=4,
                   help="segments locked concurrently; output order is preserved")
    p.add_argument("--verbose", action="store_true", help="report every segment on stderr")
    p.add_argument("--key-file")
    p.add_argument("--password")


def _verify_once(args, features, key, nonce, workdir):
    """One lock+unlock round trip. Returns (verdict, touched, repairs)."""
    import hashlib

    tag = hashlib.sha256(nonce).hexdigest()[:8]
    locked_path = os.path.join(workdir, f"locked-{tag}.bin")
    restored_path = os.path.join(workdir, f"restored-{tag}.bin")

    result = lock(
        args.input, locked_path, key=key, nonce=nonce, features=features,
        mode=args.mode, intensity=args.intensity, selftest=False, report=_report,
        # verify reports a no-op itself, below, and exits non-zero for it;
        # let it reach that line rather than raising out of lock().
        allow_noop=True,
    )
    unlock(locked_path, restored_path, key=key, manifest=result.manifest,
           report=_report)

    original = sha256_file(args.input)
    restored = sha256_file(restored_path)
    changed = sha256_file(locked_path) != original
    repairs = sum(len(l.repairs) for l in result.manifest.layers)
    touched = sum(l.slots_touched for l in result.manifest.layers)

    if original != restored:
        verdict = "MISMATCH"
    elif not changed:
        verdict = "NO-OP"
    else:
        verdict = "EXACT"
    return verdict, touched, repairs


def cmd_verify(args) -> int:
    """Lock and unlock a file in a scratch directory and report the outcome.

    The nonce is derived from the run index rather than drawn at random, so a
    verify result is reproducible. That matters more than it sounds: some
    carriers round trip for most nonces and fail for a few -- measured on an
    interlaced MPEG-4 carrier, 10 of 12 random nonces passed and 2 raised a
    geometry error. With a random nonce, a single green run was being read as
    proof the carrier was safe. Use --repeat to buy more confidence.
    """
    import hashlib
    import shutil
    import tempfile

    features = select_features(args.input, args.features.split(",") if args.features else None)
    workdir = tempfile.mkdtemp(prefix="glitchlock-verify-")
    try:
        key = load_key_file(args.key_file) if args.key_file else b"\x00" * 32
        verdicts = []
        for run in range(args.repeat):
            if args.random_nonce:
                nonce = random_nonce()
            else:
                nonce = hashlib.sha256(
                    f"glitchlock-verify|{run}".encode("utf-8")
                ).digest()[:16]
            try:
                verdict, touched, repairs = _verify_once(
                    args, features, key, nonce, workdir)
            except LockError as exc:
                # A failing nonce must not abort the sweep -- the whole point of
                # --repeat is to find the nonces that fail, so record and go on.
                verdict, touched, repairs = "FAILED", 0, 0
                _err(str(exc).splitlines()[0])
            verdicts.append(verdict)
            if args.repeat > 1:
                print(f"run {run + 1}/{args.repeat}:  {verdict}"
                      f"  ({touched} slots, {repairs} repairs)")
            else:
                print(f"features:   {', '.join(features)}")
                print(f"slots kept:  {touched} scrambled")
                print(f"repairs:     {repairs}")
                print("ciphertext differs from plaintext: "
                      f"{'yes' if verdict != 'NO-OP' else 'NO (suspicious)'}")
                print(f"round trip:  "
                      f"{'EXACT' if verdict != 'MISMATCH' else 'MISMATCH'}")

        if args.repeat > 1:
            from collections import Counter
            tally = Counter(verdicts)
            print("features:   " + ", ".join(features))
            print("result:     " + ", ".join(f"{k}={v}" for k, v in tally.most_common()))
            if len(tally) > 1:
                print("this carrier is NOT reliably lockable: the outcome depends "
                      "on the nonce, so one passing run proves nothing")
        return 0 if all(v == "EXACT" for v in verdicts) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glitchlock",
        description="Reversible, keyed bitstream scrambling for video (via FFglitch) "
                    "and MP2 audio.",
    )
    parser.add_argument("--version", action="version", version=f"glitchlock {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="show what can be locked in a file")
    p.add_argument("input")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("prepare", help="transcode any video into a glitchable carrier")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--codec", default="mpeg2video", choices=["mpeg2video", "mpeg4", "h264", "hevc"])
    p.add_argument("--qscale", type=int, default=6)
    p.add_argument("--gop", type=int, default=25)
    p.add_argument("--closed-gop", action="store_true",
                   help="self-contained GOPs, so the carrier can be split into "
                        "independently lockable stream segments")
    p.add_argument("--container", choices=ffg.CONTAINERS, default="raw",
                   help="ts: MPEG-TS with MP2 audio, for ts-lock (h264/hevc only)")
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
    p.add_argument("--features",
                   help=f"comma separated; default: all of {','.join(SUPPORTED_FEATURES)} "
                        f"present (video) or {','.join(mp2.FEATURES)} (MP2)")
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
    p.add_argument("--allow-noop", action="store_true",
                   help="permit a locked file that is byte-identical to the "
                        "carrier (i.e. nothing was scrambled). Refused by default")
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

    p = sub.add_parser("stream-lock",
                       help="lock a closed-GOP elementary stream segment by segment")
    _add_stream_io(p)
    p.add_argument("--session", required=True,
                   help="where to write the session record the receiver needs")
    p.add_argument("--features", help="comma separated; default: mv")
    p.add_argument("--mode", choices=MODES, default="full")
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument("--gops", type=int, default=1,
                   help="GOPs per segment; more = less overhead, more latency")
    p.set_defaults(func=cmd_stream_lock, recipient=None, keyless=False)

    p = sub.add_parser("stream-unlock",
                       help="restore a locked stream from its session record")
    _add_stream_io(p)
    p.add_argument("--session", required=True, help="record written by stream-lock")
    p.set_defaults(func=cmd_stream_unlock, identity=None)

    p = sub.add_parser("ts-lock",
                       help="lock the video and MP2 audio inside an MPEG-TS, keeping timestamps")
    p.add_argument("input", help="transport stream from 'prepare --container ts'")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--session", required=True,
                   help="where to write the session record the receiver needs")
    p.add_argument("--features", default="mv", help="video features, comma separated")
    p.add_argument("--audio-features", default=",".join(mp2.FEATURES),
                   help="MP2 features, comma separated, or 'none' to leave audio clear")
    p.add_argument("--mode", choices=MODES, default="full")
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument("--gops", type=int, default=1, help="GOPs per video segment")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--key-file")
    p.add_argument("--password")
    p.set_defaults(func=cmd_ts_lock, recipient=None, keyless=False)

    p = sub.add_parser("ts-unlock", help="restore a locked MPEG-TS from its session record")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--session", required=True, help="record written by ts-lock")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--key-file")
    p.add_argument("--password")
    p.set_defaults(func=cmd_ts_unlock, identity=None)

    p = sub.add_parser("verify", help="lock+unlock in a scratch dir and report exactness")
    p.add_argument("input")
    p.add_argument("--features")
    p.add_argument("--mode", default="full", choices=list(MODES))
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument("--key-file")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="run the round trip N times with different nonces. Some "
                        "carriers pass for most nonces and fail for a few, so "
                        "one run is weak evidence")
    p.add_argument("--random-nonce", action="store_true",
                   help="draw nonces at random instead of deriving them from the "
                        "run index (makes the result non-reproducible)")
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
