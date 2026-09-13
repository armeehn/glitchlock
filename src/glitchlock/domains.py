"""Per-feature value domains.

The whole scheme rests on one property: every slot we touch must have a known,
finite, *cyclic* domain that the codec reproduces exactly. If we only ever
write values drawn from that domain, and the encoder never clamps or rejects
them, then any bijection on the domain is a losslessly reversible edit.

Each supported feature therefore declares, for every slot, a domain
``(lo, n)`` meaning the integers ``lo, lo+1, ..., lo+n-1``.

Empirically verified against FFglitch 0.10.2 (see DESIGN.md "Evidence"):

``mv``
    MPEG-2 and MPEG-4 part 2 motion vectors. The reconstructed vector is stored
    modulo the f_code range: the decoder sign-extends it to a fixed width, so
    writing an out-of-range value does not clamp, it wraps. That makes the legal
    set a true cyclic group of size ``2**(f + SHIFT)``.

    The width is *not* the same in both codecs, and getting this wrong is
    silently destructive rather than loudly broken:

    ===============  =========  ==================  =========================
    codec            SHIFT      f_code=1 domain     source
    ===============  =========  ==================  =========================
    MPEG-1/2 video   ``f + 4``  ``[-16, 15]``       sign_extend(val, 4+f_code)
    MPEG-4 / H.263   ``f + 5``  ``[-32, 31]``       sign_extend(val, 5+f_code)
    ===============  =========  ==================  =========================

    A real MPEG-4 carrier will happily contain the vector 17, which is legal
    there but outside the MPEG-2 domain for the same f_code. Assuming the
    narrower width wraps it to -15 and loses information permanently, so the
    domain table below is keyed on the codec and unknown codecs are refused
    rather than guessed at.

    f_code is per frame, and on MPEG-2 also per axis.

``qscale``
    MPEG-2 quantiser scale code: a fixed-width 5-bit field, legal values
    ``1..31`` (0 is forbidden), so ``lo = 1, n = 31``.

Deliberately unsupported: ``q_dc``, ``q_dct`` and friends. DC coefficients are
differentially coded with a variable-length prefix, so an arbitrary write
changes the number of bits emitted and desynchronises the slice. Round-trip
testing showed the re-exported block structure itself changing shape. Those
features are glitch-friendly but not reversible, and glitchlock refuses them
rather than producing a manifest it cannot honour.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple

from .walk import Path, Slot, walk

#: Features glitchlock can lock and unlock losslessly.
SUPPORTED_FEATURES = ("mv", "qscale")

#: Features FFedit exposes but which are not bit-exact reversible.
REJECTED_FEATURES = {
    "q_dc": "DC coefficients are differentially coded; edits desynchronise the slice",
    "q_dct": "DCT coefficients are variable-length coded; edits desynchronise the slice",
    "q_dc_delta": "delta DC coefficients are variable-length coded",
    "q_dct_delta": "delta DCT coefficients are variable-length coded",
    "mv_delta": "MV deltas have no fixed legal range independent of the predictor",
    "mb": "macroblock types change the meaning of the data that follows",
    "gmc": "global motion changes the geometry of every subsequent vector",
    "info": "read-only informational feature",
}

#: A domain is (lo, n): the integers lo .. lo+n-1.
Domain = Tuple[int, int]

#: A located slot: (container, key, path, domain).
DomainSlot = Tuple[Any, Any, Path, Domain]

#: How many bits wider than f_code the decoder's sign extension is, per codec.
#:
#: Only ``mpeg1video``, ``mpeg2video`` and ``mpeg4`` are reachable in practice.
#: FFglitch 0.10.2 exposes no editable features at all for the rest -- run
#: ``ffedit -i`` on an H.263, MSMPEG-4, WMV or FLV1 file and it lists nothing
#: under the stream, so there is no ``mv`` payload to scramble. The entries
#: stay because the geometry is a fact about the codec and a later FFglitch
#: may expose them; ``select_features`` reports the real reason per file.
#: H.264 motion vectors have no f_code: mvd is an unbounded se(v) and the
#: patched FFglitch (see ffglitch/NOTES.md) clamps at +-32767. The domain is
#: pinned instead to +-512 px in quarter-pel, the Level 3.1 vertical range,
#: so a hardware decoder still accepts every scrambled vector. HEVC vectors
#: are quarter-pel int16 with no per-level range at all; the same span keeps
#: every reference inside the padded picture area a decoder actually fetches.
H264_MV_SPAN = 1 << 12

#: Codecs whose vectors carry no f_code and share the fixed span above.
FLAT_MV_CODECS = frozenset({"h264", "hevc"})

MV_WIDTH_SHIFT = {
    "mpeg1video": 4,
    "mpeg2video": 4,
    "mpeg4": 5,
    "h263": 5,
    "h263p": 5,
    "msmpeg4v1": 5,
    "msmpeg4v2": 5,
    "msmpeg4v3": 5,
    "wmv1": 5,
    "wmv2": 5,
    "flv1": 5,
}

#: Features each codec can have locked. Anything absent is refused.
CODEC_FEATURES = {
    "h264": ("mv",),
    "hevc": ("mv",),
    "mpeg1video": ("mv", "qscale"),
    "mpeg2video": ("mv", "qscale"),
    "mpeg4": ("mv",),
    "h263": ("mv",),
    "h263p": ("mv",),
    "msmpeg4v1": ("mv",),
    "msmpeg4v2": ("mv",),
    "msmpeg4v3": ("mv",),
    "wmv1": ("mv",),
    "wmv2": ("mv",),
    "flv1": ("mv",),
}


class UnsupportedFeature(ValueError):
    pass


class UnsupportedCodec(ValueError):
    pass


class DomainViolation(ValueError):
    """A plaintext value sits outside the domain we believe the codec uses.

    This is a refusal, not a warning. Continuing would wrap the value and
    destroy it, and the whole point of glitchlock is that it does not do that.
    """


def mv_width_shift(codec: str) -> int:
    try:
        return MV_WIDTH_SHIFT[codec]
    except KeyError:
        raise UnsupportedCodec(
            f"codec {codec!r} has no verified motion vector domain. glitchlock "
            "refuses to guess: an incorrect domain silently destroys data. "
            f"Verified codecs: {', '.join(sorted(MV_WIDTH_SHIFT))}."
        ) from None


def _mv_slots(frame_payload: Dict[str, Any], codec: str) -> Iterator[DomainSlot]:
    fcode: List[int] = frame_payload.get("fcode") or []
    # Backward vectors in a B-frame are coded against their own f_code, which
    # FFedit reports separately as "bcode". They are frequently different --
    # measured on a -bf 2 mpeg2video encode, 27 of 32 B-frames had
    # bcode != fcode. Using fcode for both directions is wrong in both
    # directions: too narrow and the domain guard refuses a legal file, too
    # wide and we write a value the codec cannot store (measured: 17 written
    # into a bcode=1 slot came back as -15). P-frames carry no bcode, so fall
    # back to fcode there.
    bcode: List[int] = frame_payload.get("bcode") or fcode
    if codec in FLAT_MV_CODECS:
        fcode = bcode = [0]
    if not fcode:
        return
    shift = 0 if codec in FLAT_MV_CODECS else mv_width_shift(codec)
    for direction, codes in (("forward", fcode), ("backward", bcode)):
        grid = frame_payload.get(direction)
        if not grid:
            continue
        for container, key, rel in walk(grid):
            if codec in FLAT_MV_CODECS:
                n = H264_MV_SPAN
                yield (container, key, (direction,) + rel, (-(n >> 1), n))
                continue
            # The innermost list is the [x, y] pair, so the final path element
            # is the axis. MPEG-4 reports a single f_code for both axes;
            # MPEG-2 reports one per axis.
            axis = rel[-1] if isinstance(rel[-1], int) else 0
            f = codes[axis] if axis < len(codes) else codes[0]
            n = 1 << (f + shift)
            yield (container, key, (direction,) + rel, (-(n >> 1), n))


def _qscale_slots(frame_payload: Dict[str, Any], codec: str) -> Iterator[DomainSlot]:
    for container, key, rel in walk(frame_payload):
        yield (container, key, rel, (1, 31))


_SLOT_FINDERS = {
    "mv": _mv_slots,
    "qscale": _qscale_slots,
}


def frame_slots(feature: str, frame_payload: Any, codec: str) -> List[DomainSlot]:
    """Return every editable slot of *feature* in one frame, in canonical order."""
    if feature not in _SLOT_FINDERS:
        why = REJECTED_FEATURES.get(feature)
        if why:
            raise UnsupportedFeature(f"feature {feature!r} is not reversible: {why}")
        raise UnsupportedFeature(f"feature {feature!r} is not supported")
    allowed = CODEC_FEATURES.get(codec)
    if allowed is not None and feature not in allowed:
        raise UnsupportedFeature(
            f"feature {feature!r} is not verified reversible for codec {codec!r}"
        )
    if not frame_payload:
        return []
    return list(_SLOT_FINDERS[feature](frame_payload, codec))


def assert_in_domain(slots: List[DomainSlot], feature: str, codec: str, where: str) -> None:
    """Refuse to proceed if any value already sits outside its own domain.

    If this fires, the domain table above is wrong for this codec, and locking
    would wrap the offending value and lose it forever. Better to stop.
    """
    for container, key, path, domain in slots:
        value = container[key]
        if not check_in_domain(value, domain):
            lo, n = domain
            raise DomainViolation(
                f"{where}: {feature} value {value} at {'/'.join(str(p) for p in path)} "
                f"is outside the domain [{lo}, {lo + n - 1}] this build believes "
                f"{codec!r} uses. Locking would wrap it and destroy it, so glitchlock "
                "is stopping. Please report this with the codec and FFglitch version."
            )


def check_in_domain(value: int, domain: Domain) -> bool:
    lo, n = domain
    return lo <= value < lo + n
