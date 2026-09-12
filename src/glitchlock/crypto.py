"""Key derivation and the deterministic keystream.

Nothing here is novel and that is the point: the keystream is HMAC-SHA256 in
counter mode, which is a standard PRF construction. The security claim we make
is narrow -- see SECURITY.md -- but the *determinism* claim is absolute: given
the same key, nonce and label, ``KeyStream`` produces the same bytes on any
machine and any Python version, which is what makes unlocking possible.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import List

KEY_BYTES = 32
NONCE_BYTES = 16
SALT_BYTES = 16

# scrypt parameters. n=2**15 keeps interactive use comfortable (~100ms) while
# still costing a lot per guess.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1


def random_nonce() -> bytes:
    return secrets.token_bytes(NONCE_BYTES)


def random_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def random_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def derive_key_from_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
        maxmem=256 * 1024 * 1024,
    )


def load_key_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw:
        raise ValueError(f"key file {path!r} is empty")
    # A key file may hold raw bytes or hex/base-ish text; hash whatever it is
    # down to a fixed 32 bytes so any file works as a key.
    return hashlib.sha256(raw).digest()


def key_from_env(var: str = "GLITCHLOCK_KEY") -> bytes | None:
    val = os.environ.get(var)
    if not val:
        return None
    return hashlib.sha256(val.encode("utf-8")).digest()


class KeyStream:
    """HMAC-SHA256 counter-mode PRF with unbiased integer sampling.

    ``label`` domain-separates independent streams so that, for example, the
    substitution offsets for frame 7 of the ``mv`` layer can never collide with
    the permutation randomness for frame 7 of the ``qscale`` layer.
    """

    def __init__(self, key: bytes, nonce: bytes, label: bytes) -> None:
        self._key = key
        self._prefix = (
            len(nonce).to_bytes(2, "big") + nonce +
            len(label).to_bytes(2, "big") + label
        )
        self._counter = 0
        self._buf = b""
        self._pos = 0

    def _refill(self) -> None:
        block = hmac.new(
            self._key,
            self._prefix + self._counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        self._counter += 1
        self._buf = block
        self._pos = 0

    def read(self, count: int) -> bytes:
        # Fast path: the draw fits in the current block. randbelow asks for
        # one or two bytes at a time, so this is nearly every call.
        end = self._pos + count
        if end <= len(self._buf):
            start = self._pos
            self._pos = end
            return self._buf[start:end]

        out = bytearray()
        while len(out) < count:
            if self._pos >= len(self._buf):
                self._refill()
            take = min(count - len(out), len(self._buf) - self._pos)
            out += self._buf[self._pos:self._pos + take]
            self._pos += take
        return bytes(out)

    def randbelow(self, n: int) -> int:
        """Uniform integer in ``[0, n)`` with rejection sampling (no modulo bias)."""
        if n <= 1:
            return 0
        k = (n - 1).bit_length()
        nbytes = (k + 7) // 8
        mask = (1 << k) - 1
        while True:
            value = int.from_bytes(self.read(nbytes), "big") & mask
            if value < n:
                return value

    def permutation(self, size: int) -> List[int]:
        """Keyed Fisher-Yates shuffle of ``range(size)``.

        Returns ``perm`` such that ``dst[i] = src[perm[i]]``.
        """
        perm = list(range(size))
        for i in range(size - 1, 0, -1):
            j = self.randbelow(i + 1)
            perm[i], perm[j] = perm[j], perm[i]
        return perm


def invert_permutation(perm: List[int]) -> List[int]:
    inverse = [0] * len(perm)
    for i, p in enumerate(perm):
        inverse[p] = i
    return inverse


def mac_bytes(key: bytes, payload: bytes) -> str:
    return hmac.new(key, b"glitchlock-manifest-v1" + payload, hashlib.sha256).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
