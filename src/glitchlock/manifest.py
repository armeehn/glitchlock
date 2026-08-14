"""The manifest: the record of how a file was scrambled.

A manifest never contains the key. It contains everything *else* needed to
unwind: the nonce, the layer order and parameters, the KDF salt, integrity
digests of both the plaintext carrier and the ciphertext, and any repairs.

In keyed mode you need manifest + key. In ``--keyless`` mode the seed is stored
in the manifest in the clear, so the manifest alone unwinds the file -- useful
when the point is reversible glitch art rather than secrecy.

The manifest carries an HMAC over its own canonical serialisation, so tampering
with a layer parameter is detected before it can produce silent garbage.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .crypto import constant_time_eq, mac_bytes

MANIFEST_VERSION = "1"
FORMAT_NAME = "glitchlock-manifest"


@dataclass
class Layer:
    feature: str
    mode: str
    intensity: float
    frames: int = 0
    slots_total: int = 0
    slots_touched: int = 0
    buckets: int = 0
    #: address -> original plaintext value, for slots the codec did not
    #: reproduce exactly. Empirically empty for mv and qscale.
    repairs: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Pin every numeric field's type, so the MAC input cannot drift.

        The MAC is taken over ``json.dumps`` of this record, so the tag
        depends on how Python *renders* each number, not only on its value.
        JSON has a single number type and so does JavaScript, so a manifest
        that travels through a browser -- which the web UI's "send to unlock"
        does -- comes back with ``intensity`` 1.0 re-serialised as ``1``. Same
        number, different byte string, so the tag stops matching and the user
        is told their key is wrong or the manifest was altered. Both are
        false, and nothing in the message hints that the manifest merely took
        a different route home.

        Coercing here is backward compatible: every manifest this tool has
        written already holds a float intensity and integer counts, so their
        canonical bytes are unchanged.
        """
        self.intensity = float(self.intensity)
        self.frames = int(self.frames)
        self.slots_total = int(self.slots_total)
        self.slots_touched = int(self.slots_touched)
        self.buckets = int(self.buckets)


@dataclass
class Manifest:
    format: str = FORMAT_NAME
    version: str = MANIFEST_VERSION
    tool: str = "glitchlock"
    ffglitch: str = ""
    codec: str = ""
    nonce: str = ""
    #: Streaming segment number. Zero for an ordinary single-file lock. When a
    #: stream is cut into independently locked pieces, each piece needs its own
    #: number so the pieces do not share a keystream.
    segment: int = 0
    keyless: bool = False
    #: present only when keyless: the seed the key was derived from
    seed: Optional[str] = None
    kdf: Optional[Dict[str, Any]] = None
    #: present when locked to public keys: the wrapped content key per recipient.
    #: Holds no secret on its own -- opening it needs a matching private key.
    kem: Optional[Dict[str, Any]] = None
    carrier_sha256: str = ""
    carrier_bytes: int = 0
    locked_sha256: str = ""
    locked_bytes: int = 0
    layers: List[Layer] = field(default_factory=list)
    selftest: str = "not-run"
    mac: str = ""

    # ---------------------------------------------------------------- codec

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["layers"] = [asdict(l) if not isinstance(l, dict) else l for l in self.layers]
        return data

    def canonical_bytes(self) -> bytes:
        """Serialisation used for the MAC: every field except ``mac`` itself."""
        data = self.to_dict()
        data.pop("mac", None)
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def sign(self, key: bytes) -> None:
        self.mac = mac_bytes(key, self.canonical_bytes())

    def verify(self, key: bytes) -> bool:
        if not self.mac:
            return False
        return constant_time_eq(self.mac, mac_bytes(key, self.canonical_bytes()))

    def save(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")

    @classmethod
    def load(cls, path: str) -> "Manifest":
        with open(path, "r") as fh:
            data = json.load(fh)
        if data.get("format") != FORMAT_NAME:
            raise ValueError(f"{path!r} is not a glitchlock manifest")
        if data.get("version") != MANIFEST_VERSION:
            raise ValueError(
                f"manifest version {data.get('version')!r} is not supported by this "
                f"build (expected {MANIFEST_VERSION!r})"
            )
        layers = [Layer(**l) for l in data.pop("layers", [])]
        return cls(layers=layers, **data)
