"""Public-key recipients: lock to someone's public key, unlock with their private key.

The asymmetric key never touches the video. This is ordinary hybrid encryption
(KEM-DEM), the same shape ``age`` and ``PGP`` use:

1. A random 32-byte **content key** is generated per lock. That is the key the
   scrambler actually runs on, and the key the manifest is MAC'd under.
2. For each recipient, an ephemeral X25519 keypair is generated, X25519 is run
   against the recipient's public key, HKDF-SHA256 turns the shared secret into
   a wrapping key, and the content key is sealed under it with
   ChaCha20-Poly1305.
3. The manifest carries the ephemeral public keys and the wrapped content keys.
   It never carries the content key itself.

Unlocking runs the same X25519 in the other direction with the recipient's
private key, recovers the wrapping key, and unseals the content key.

Because a fresh ephemeral keypair is used per recipient per lock, the same file
can be locked to several people at once, and locking twice to the same recipient
produces unrelated wrappings.

**What this buys you:** key distribution. You can lock a file, or a live stream,
for someone who has published a public key, with no shared secret and no prior
contact.

**What it does not buy you:** any change to what the ciphertext leaks. The locked
video is still a playable video whose residual picture survives. This layer
protects the content key, not the footage. See SECURITY.md.

Requires the ``cryptography`` package: ``pip install glitchlock[recipients]``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import Any, Dict, List, Sequence, Tuple

KEM_ALGO = "x25519-hkdf-sha256-chacha20poly1305"
KEM_INFO = b"glitchlock-kem-v1"

SECRET_PREFIX = "glk-sec-v1:"
PUBLIC_PREFIX = "glk-pub-v1:"

CONTENT_KEY_BYTES = 32


class PubKeyError(ValueError):
    pass


class MissingDependency(PubKeyError):
    pass


class NotARecipient(PubKeyError):
    """This identity does not appear among the manifest's recipients."""


def _backend():
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey, X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - exercised by hand
        raise MissingDependency(
            "public-key recipients need the 'cryptography' package. Install it "
            "with: pip install 'glitchlock[recipients]'"
        ) from exc
    return X25519PrivateKey, X25519PublicKey, ChaCha20Poly1305, HKDF, hashes


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text.strip(), validate=True)
    except Exception as exc:
        raise PubKeyError(f"not valid base64: {text[:24]!r}") from exc


# ------------------------------------------------------------------ identities


def generate_identity() -> Tuple[str, str]:
    """Return ``(secret, public)`` as copy-pasteable single-line tokens."""
    X25519PrivateKey, _pub, _aead, _hkdf, _h = _backend()
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    sk = X25519PrivateKey.generate()
    raw_sk = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_pk = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return SECRET_PREFIX + _b64(raw_sk), PUBLIC_PREFIX + _b64(raw_pk)


def _secret_raw(secret: str) -> bytes:
    secret = secret.strip()
    if not secret.startswith(SECRET_PREFIX):
        raise PubKeyError(
            f"not a glitchlock secret key (expected a {SECRET_PREFIX!r} prefix)")
    raw = _unb64(secret[len(SECRET_PREFIX):])
    if len(raw) != 32:
        raise PubKeyError(f"secret key must be 32 bytes, got {len(raw)}")
    return raw


def _public_raw(public: str) -> bytes:
    public = public.strip()
    if not public.startswith(PUBLIC_PREFIX):
        raise PubKeyError(
            f"not a glitchlock public key (expected a {PUBLIC_PREFIX!r} prefix)")
    raw = _unb64(public[len(PUBLIC_PREFIX):])
    if len(raw) != 32:
        raise PubKeyError(f"public key must be 32 bytes, got {len(raw)}")
    return raw


def public_from_secret(secret: str) -> str:
    X25519PrivateKey, _pub, _aead, _hkdf, _h = _backend()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    sk = X25519PrivateKey.from_private_bytes(_secret_raw(secret))
    raw_pk = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return PUBLIC_PREFIX + _b64(raw_pk)


def fingerprint(public: str) -> str:
    """Short, stable identifier for a public key. Not a secret."""
    return hashlib.sha256(_public_raw(public)).hexdigest()[:16]


def read_key_file(path: str) -> str:
    """Read a key token from a file, ignoring blank lines and ``#`` comments."""
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    raise PubKeyError(f"no key found in {path!r}")


def write_identity(path: str, secret: str, public: str) -> None:
    """Write a secret key file with 0600 permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("# glitchlock identity - keep this file secret\n")
        fh.write(f"# public key: {public}\n")
        fh.write(f"# fingerprint: {fingerprint(public)}\n")
        fh.write(secret + "\n")


# --------------------------------------------------------------------- sealing


def new_content_key() -> bytes:
    return secrets.token_bytes(CONTENT_KEY_BYTES)


def seal(content_key: bytes, recipients: Sequence[str]) -> Dict[str, Any]:
    """Wrap *content_key* for each recipient. Returns the manifest ``kem`` block."""
    if not recipients:
        raise PubKeyError("at least one recipient is required")
    X25519PrivateKey, X25519PublicKey, ChaCha20Poly1305, HKDF, hashes = _backend()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    entries: List[Dict[str, str]] = []
    seen = set()
    for public in recipients:
        raw_pk = _public_raw(public)
        if raw_pk in seen:
            continue
        seen.add(raw_pk)

        pk = X25519PublicKey.from_public_bytes(raw_pk)
        esk = X25519PrivateKey.generate()
        raw_epk = esk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        shared = esk.exchange(pk)

        wrap = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=KEM_INFO + raw_epk + raw_pk).derive(shared)
        nonce = os.urandom(12)
        blob = ChaCha20Poly1305(wrap).encrypt(nonce, content_key, KEM_INFO)

        entries.append({
            "fp": fingerprint(public),
            "epk": _b64(raw_epk),
            "wrapped": _b64(nonce + blob),
        })

    return {"algo": KEM_ALGO, "recipients": entries}


def unseal(kem: Dict[str, Any], secret: str) -> bytes:
    """Recover the content key from a ``kem`` block using *secret*."""
    if not kem:
        raise PubKeyError("this manifest has no recipient block")
    if kem.get("algo") != KEM_ALGO:
        raise PubKeyError(f"unsupported recipient algorithm {kem.get('algo')!r}")

    X25519PrivateKey, X25519PublicKey, ChaCha20Poly1305, HKDF, hashes = _backend()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from cryptography.exceptions import InvalidTag

    sk = X25519PrivateKey.from_private_bytes(_secret_raw(secret))
    raw_pk = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    mine = hashlib.sha256(raw_pk).hexdigest()[:16]

    entries = kem.get("recipients") or []
    # Try the entry matching our fingerprint first, then everything else: the
    # fingerprint is a hint for speed, never the thing that grants access.
    ordered = ([e for e in entries if e.get("fp") == mine] +
               [e for e in entries if e.get("fp") != mine])

    for entry in ordered:
        try:
            raw_epk = _unb64(entry["epk"])
            packed = _unb64(entry["wrapped"])
        except (PubKeyError, KeyError):
            continue
        if len(raw_epk) != 32 or len(packed) < 13:
            continue
        try:
            shared = sk.exchange(X25519PublicKey.from_public_bytes(raw_epk))
            wrap = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                        info=KEM_INFO + raw_epk + raw_pk).derive(shared)
            return ChaCha20Poly1305(wrap).decrypt(packed[:12], packed[12:], KEM_INFO)
        except (InvalidTag, ValueError):
            continue

    raise NotARecipient(
        "this identity cannot open the file: it is not among the "
        f"{len(entries)} recipient(s) this manifest was locked for"
    )


def recipient_fingerprints(kem: Dict[str, Any]) -> List[str]:
    return [e.get("fp", "?") for e in (kem or {}).get("recipients", [])]
