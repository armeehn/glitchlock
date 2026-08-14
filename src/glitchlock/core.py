"""Lock and unlock pipelines.

The pipeline for a single feature layer is:

    export -> transform -> apply -> re-export -> diff

That fourth and fifth step are the part that makes the manifest trustworthy.
We never assume the encoder wrote what we asked for; we read the ciphertext
back and compare it against what we intended. Anything that disagrees is
recorded as a *repair* -- the original plaintext value, stored in the manifest
-- so recovery stays exact even if a codec edge case bites. On the supported
features this list comes back empty, which is the result you want but not one
you should take on faith.

By default `lock` then performs a full self-test: it unlocks its own output in
a temporary directory and compares the SHA-256 against the input carrier. If
that does not match, no manifest is written. A lock that cannot be proven
reversible is not shipped.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from . import ffg
from .crypto import sha256_file
from .domains import CODEC_FEATURES, REJECTED_FEATURES, SUPPORTED_FEATURES
from .manifest import Layer, Manifest
from .transform import (
    apply_repairs,
    collect_values,
    transform_document,
)

Reporter = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


class LockError(RuntimeError):
    pass


class GeometryError(LockError):
    """The ciphertext no longer has the same slot layout as the plaintext."""


class NoOpLock(LockError):
    """The lock produced a byte-identical copy of the carrier.

    Reversibility is trivially satisfied by a copy, so the self-test cannot
    catch this. It is nevertheless the worst outcome the tool has: the user
    is handed the plaintext and told it is locked.
    """


@dataclass
class LockResult:
    manifest: Manifest
    locked_path: str
    selftest_ok: Optional[bool]


def lockable_features(path: str) -> List[str]:
    """Features that are both exposed by FFedit and verified for this codec."""
    available = ffg.supported_features(path)
    codec = ffg.codec_name(path)
    verified = CODEC_FEATURES.get(codec, ())
    return [f for f in SUPPORTED_FEATURES if f in available and f in verified]


def select_features(path: str, requested: Optional[Sequence[str]]) -> List[str]:
    """Resolve the feature list for *path*, validating against FFedit and codec."""
    available = ffg.supported_features(path)
    codec = ffg.codec_name(path)
    verified = CODEC_FEATURES.get(codec)

    if requested:
        chosen = list(requested)
        for feature in chosen:
            if feature in REJECTED_FEATURES:
                raise LockError(
                    f"feature {feature!r} is not bit-exact reversible "
                    f"({REJECTED_FEATURES[feature]}); glitchlock refuses to use it"
                )
            if feature not in SUPPORTED_FEATURES:
                raise LockError(
                    f"feature {feature!r} is not supported; supported features are "
                    f"{', '.join(SUPPORTED_FEATURES)}"
                )
            if available and feature not in available:
                raise LockError(
                    f"this file does not expose {feature!r}; FFedit reports: "
                    f"{', '.join(available) or 'nothing'}"
                )
            if verified is not None and feature not in verified:
                raise LockError(
                    f"feature {feature!r} is not verified reversible for codec "
                    f"{codec!r}; verified here: {', '.join(verified) or 'none'}"
                )
        return chosen

    if verified is None:
        raise LockError(
            f"codec {codec!r} has no verified reversible feature in this build. "
            f"Verified codecs: {', '.join(sorted(CODEC_FEATURES))}. "
            "Run 'glitchlock prepare' to build a glitchable carrier first."
        )
    chosen = [f for f in SUPPORTED_FEATURES if f in available and f in verified]
    if not chosen:
        raise LockError(
            f"no reversible feature available for this {codec!r} file "
            f"(FFedit reports: {', '.join(available) or 'nothing'}). "
            "Run 'glitchlock prepare' to build a glitchable carrier first."
        )
    return chosen


def _run_layer(
    src: str,
    dst: str,
    feature: str,
    key: bytes,
    nonce: bytes,
    mode: str,
    intensity: float,
    forward: bool,
    workdir: str,
    repairs: Optional[Dict[str, int]] = None,
    segment: int = 0,
) -> Layer:
    """Transform one feature from *src* into *dst*, returning layer statistics."""
    exported = os.path.join(workdir, f"{feature}.export.json")
    modified = os.path.join(workdir, f"{feature}.modified.json")

    doc = ffg.export(src, feature, exported)
    before = collect_values(doc, feature)

    stats = transform_document(
        doc, feature, key, nonce, mode=mode, intensity=intensity,
        forward=forward, segment=segment,
    )

    if not forward and repairs:
        apply_repairs(doc, feature, repairs)

    intended = collect_values(doc, feature)
    ffg.write_json(doc, modified)
    ffg.apply(src, feature, modified, dst)

    layer = Layer(
        feature=feature,
        mode=mode,
        intensity=intensity,
        frames=stats.frames,
        slots_total=stats.slots_total,
        slots_touched=stats.slots_touched,
        buckets=stats.buckets,
    )

    if forward:
        # Read the ciphertext back and record anything the encoder did not
        # reproduce, so unlock can repair it from the manifest.
        verify_json = os.path.join(workdir, f"{feature}.verify.json")
        actual_doc = ffg.export(dst, feature, verify_json)
        actual = collect_values(actual_doc, feature)
        if set(actual) != set(intended):
            raise GeometryError(
                f"feature {feature!r}: the locked file has a different slot layout "
                f"({len(actual)} slots vs {len(intended)}). This codec/feature "
                "combination is not reversible."
            )
        layer.repairs = {
            address: before[address]
            for address, value in intended.items()
            if actual[address] != value
        }

    return layer


def lock(
    carrier: str,
    out_path: str,
    key: bytes,
    nonce: bytes,
    features: Sequence[str],
    mode: str = "full",
    intensity: float = 1.0,
    selftest: bool = True,
    report: Reporter = _noop,
    segment: int = 0,
    allow_noop: bool = False,
) -> LockResult:
    manifest = Manifest(
        ffglitch=ffg.version(),
        codec=ffg.codec_name(carrier),
        nonce=nonce.hex(),
        segment=segment,
        carrier_sha256=sha256_file(carrier),
        carrier_bytes=os.path.getsize(carrier),
    )

    workroot = tempfile.mkdtemp(prefix="glitchlock-lock-")
    try:
        current = carrier
        for index, feature in enumerate(features):
            is_last = index == len(features) - 1
            dst = out_path if is_last else os.path.join(workroot, f"stage{index}.bin")
            layer_dir = os.path.join(workroot, f"layer{index}")
            os.makedirs(layer_dir, exist_ok=True)
            report(f"  locking layer {index + 1}/{len(features)}: {feature}")
            layer = _run_layer(
                current, dst, feature, key, nonce, mode, intensity,
                forward=True, workdir=layer_dir, segment=segment,
            )
            report(
                f"    {layer.slots_touched}/{layer.slots_total} slots across "
                f"{layer.frames} frames"
                + (f", {len(layer.repairs)} repairs" if layer.repairs else "")
            )
            manifest.layers.append(layer)
            current = dst
    finally:
        shutil.rmtree(workroot, ignore_errors=True)

    manifest.locked_sha256 = sha256_file(out_path)
    manifest.locked_bytes = os.path.getsize(out_path)

    if not allow_noop and manifest.locked_sha256 == manifest.carrier_sha256:
        # The self-test below would pass this happily -- a copy unlocks to
        # itself byte for byte -- so reversibility is the wrong question to
        # ask here. The right one is whether anything was scrambled at all.
        # Two ways to land here, both silent before this check:
        #   * the carrier has no slots for the chosen features (an all-intra
        #     MPEG-4 file has no motion vectors at all), and
        #   * every value in a permutation bucket is identical, so shuffling
        #     them is the identity map (constant-qscale --mode permute).
        raise NoOpLock(
            "the locked file is byte-identical to the carrier: nothing was "
            "actually scrambled, so this output is the plaintext. "
            f"{sum(l.slots_touched for l in manifest.layers)} slots were "
            f"reported touched across features "
            f"{', '.join(l.feature for l in manifest.layers) or 'none'}. "
            "An all-intra carrier has no motion vectors to scramble, and "
            "permuting values that are all equal is the identity. Re-encode "
            "with a GOP longer than 1, choose different --features/--mode, or "
            "pass --allow-noop if you really want a copy. No manifest written."
        )

    selftest_ok: Optional[bool] = None
    if selftest:
        report("  self-test: unlocking own output")
        probe_dir = tempfile.mkdtemp(prefix="glitchlock-selftest-")
        try:
            probe = os.path.join(probe_dir, "restored.bin")
            unlock(out_path, probe, key, manifest, verify_input=False, report=_noop)
            selftest_ok = sha256_file(probe) == manifest.carrier_sha256
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
        manifest.selftest = "pass" if selftest_ok else "FAIL"
        if not selftest_ok:
            raise LockError(
                "self-test failed: unlocking the locked file did not reproduce the "
                "carrier byte for byte. No manifest was written. Please report this "
                "with the codec and FFglitch version."
            )
        report("  self-test: pass (byte-exact)")

    manifest.sign(key)
    return LockResult(manifest=manifest, locked_path=out_path, selftest_ok=selftest_ok)


def unlock(
    locked: str,
    out_path: str,
    key: bytes,
    manifest: Manifest,
    verify_input: bool = True,
    report: Reporter = _noop,
) -> str:
    if verify_input:
        actual = sha256_file(locked)
        if manifest.locked_sha256 and actual != manifest.locked_sha256:
            raise LockError(
                "the locked file does not match this manifest\n"
                f"  manifest expects: {manifest.locked_sha256}\n"
                f"  file is:          {actual}"
            )

    layers = list(manifest.layers)
    if not layers:
        shutil.copyfile(locked, out_path)
        return out_path

    nonce = bytes.fromhex(manifest.nonce)
    workroot = tempfile.mkdtemp(prefix="glitchlock-unlock-")
    try:
        current = locked
        for index, layer in enumerate(reversed(layers)):
            is_last = index == len(layers) - 1
            dst = out_path if is_last else os.path.join(workroot, f"stage{index}.bin")
            layer_dir = os.path.join(workroot, f"layer{index}")
            os.makedirs(layer_dir, exist_ok=True)
            report(f"  unlocking layer {len(layers) - index}/{len(layers)}: {layer.feature}")
            _run_layer(
                current, dst, layer.feature, key, nonce, layer.mode, layer.intensity,
                forward=False, workdir=layer_dir, repairs=layer.repairs,
                segment=manifest.segment,
            )
            current = dst
    finally:
        shutil.rmtree(workroot, ignore_errors=True)
    return out_path
