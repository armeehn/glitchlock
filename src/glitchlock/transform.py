"""The reversible transform itself.

Two keyed layers are composed per frame, both bijections on the slot domains:

**Substitution** -- each slot gets its own keystream offset and is shifted
inside its own cyclic domain::

    lock:    v' = lo + ((v  - lo) + k) mod n
    unlock:  v  = lo + ((v' - lo) - k) mod n

**Permutation** -- slots are bucketed by identical domain and shuffled within
their bucket with a keyed Fisher-Yates. Bucketing matters: moving a value from
a frame with f_code 5 into a frame with f_code 1 would push it outside the
smaller domain and the codec would wrap it, destroying information. Values only
ever move between slots that can represent them.

Both layers are planned from the keystream *before* any value is read, so the
plan depends only on (key, nonce, mode, intensity, slot geometry) and never on
the plaintext. That is what lets `unlock` rebuild the identical plan from the
ciphertext, whose slot geometry is unchanged by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Tuple

from .crypto import KeyStream, invert_permutation
from .domains import Domain, DomainSlot, assert_in_domain, frame_slots

MODES = ("full", "substitute", "permute")


@dataclass
class FramePlan:
    """The data-independent recipe for one frame."""

    selected: List[int] = field(default_factory=list)          # indices into the slot list
    offsets: List[int] = field(default_factory=list)           # one per selected slot
    buckets: List[Tuple[Domain, List[int], List[int]]] = field(default_factory=list)
    # each bucket is (domain, positions-within-selected, permutation)


@dataclass
class LayerStats:
    frames: int = 0
    slots_total: int = 0
    slots_touched: int = 0
    buckets: int = 0


def iter_frames(
    doc: Dict[str, Any], feature: str
) -> Iterator[Tuple[int, int, str, Any]]:
    """Yield ``(stream_index, frame_index, codec, feature_payload)`` per frame.

    The codec comes from the FFedit document itself rather than being passed in,
    so the value domains are always derived from the same file being edited.
    """
    for si, stream in enumerate(doc.get("streams") or []):
        codec = stream.get("codec") or ""
        for fi, frame in enumerate(stream.get("frames") or []):
            yield si, fi, codec, frame.get(feature)


def _label(
    feature: str, stream_idx: int, frame_idx: int, part: str, segment: int = 0
) -> bytes:
    """Domain-separation label for one frame's keystream.

    ``segment`` exists for streaming. When a long stream is cut into
    independently processed pieces, each piece's frame numbering restarts at
    zero, so frame 0 of every piece would derive the *same* plan from the same
    key and nonce -- classic keystream reuse. Passing a distinct segment number
    per piece separates them.

    Segment 0 keeps the original four-field label, so non-streaming use is
    unchanged. Segment > 0 inserts a fifth field. The two forms can never
    collide because they differ in field count.
    """
    if segment:
        return f"{feature}|{segment}|{stream_idx}|{frame_idx}|{part}".encode("utf-8")
    return f"{feature}|{stream_idx}|{frame_idx}|{part}".encode("utf-8")


def plan_frame(
    slots: List[DomainSlot],
    key: bytes,
    nonce: bytes,
    feature: str,
    stream_idx: int,
    frame_idx: int,
    mode: str,
    intensity: float,
    segment: int = 0,
) -> FramePlan:
    """Build the keyed, data-independent plan for one frame."""
    plan = FramePlan()
    if not slots:
        return plan

    ks = KeyStream(key, nonce, _label(feature, stream_idx, frame_idx, "plan", segment))

    # 1. Selection. intensity == 1.0 short-circuits so the common case does not
    #    burn keystream on a decision whose answer is always yes.
    if intensity >= 1.0:
        plan.selected = list(range(len(slots)))
    else:
        threshold = int(intensity * (1 << 24))
        plan.selected = [
            i for i in range(len(slots))
            if ks.randbelow(1 << 24) < threshold
        ]
    if not plan.selected:
        return plan

    # 2. Substitution offsets, one per selected slot, drawn from that slot's own
    #    domain size.
    if mode in ("full", "substitute"):
        plan.offsets = [ks.randbelow(slots[i][3][1]) for i in plan.selected]

    # 3. Permutation buckets, keyed by domain so a value never changes domain.
    if mode in ("full", "permute"):
        by_domain: Dict[Domain, List[int]] = {}
        for pos, slot_index in enumerate(plan.selected):
            by_domain.setdefault(slots[slot_index][3], []).append(pos)
        for domain in sorted(by_domain):
            positions = by_domain[domain]
            plan.buckets.append((domain, positions, ks.permutation(len(positions))))

    return plan


def _read(slots: List[DomainSlot], index: int) -> int:
    container, key, _path, _domain = slots[index]
    return container[key]


def _write(slots: List[DomainSlot], index: int, value: int) -> None:
    container, key, _path, _domain = slots[index]
    container[key] = value


def apply_plan(slots: List[DomainSlot], plan: FramePlan, forward: bool) -> int:
    """Apply *plan* in place. Returns the number of slots touched.

    ``forward=True`` locks (substitute, then permute).
    ``forward=False`` unlocks (inverse permute, then inverse substitute).
    """
    if not plan.selected:
        return 0

    def substitute(sign: int) -> None:
        for pos, slot_index in enumerate(plan.selected):
            if pos >= len(plan.offsets):
                break
            lo, n = slots[slot_index][3]
            value = _read(slots, slot_index)
            _write(slots, slot_index, lo + ((value - lo) + sign * plan.offsets[pos]) % n)

    def permute(inverse: bool) -> None:
        for _domain, positions, perm in plan.buckets:
            order = invert_permutation(perm) if inverse else perm
            values = [_read(slots, plan.selected[p]) for p in positions]
            shuffled = [values[p] for p in order]
            for p, value in zip(positions, shuffled):
                _write(slots, plan.selected[p], value)

    if forward:
        substitute(+1)
        permute(False)
    else:
        permute(True)
        substitute(-1)

    return len(plan.selected)


def transform_document(
    doc: Dict[str, Any],
    feature: str,
    key: bytes,
    nonce: bytes,
    mode: str = "full",
    intensity: float = 1.0,
    forward: bool = True,
    segment: int = 0,
) -> LayerStats:
    """Lock or unlock one FFedit feature document in place."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    if not 0.0 < intensity <= 1.0:
        raise ValueError("intensity must be in (0, 1]")

    stats = LayerStats()
    for stream_idx, frame_idx, codec, payload in iter_frames(doc, feature):
        slots = frame_slots(feature, payload, codec)
        if not slots:
            continue
        if forward:
            # Guard the assumption the whole scheme rests on, on real data,
            # before we touch a single value.
            assert_in_domain(
                slots, feature, codec, f"stream {stream_idx} frame {frame_idx}"
            )
        stats.frames += 1
        stats.slots_total += len(slots)
        plan = plan_frame(
            slots, key, nonce, feature, stream_idx, frame_idx, mode, intensity, segment
        )
        stats.buckets += len(plan.buckets)
        stats.slots_touched += apply_plan(slots, plan, forward)
    return stats


def collect_values(doc: Dict[str, Any], feature: str) -> Dict[str, int]:
    """Snapshot every slot value keyed by a stable address string.

    Used to diff an intended payload against what the encoder actually wrote,
    so any discrepancy can be recorded in the manifest as a repair.
    """
    out: Dict[str, int] = {}
    for stream_idx, frame_idx, codec, payload in iter_frames(doc, feature):
        for container, key, path, _domain in frame_slots(feature, payload, codec):
            address = f"{stream_idx}/{frame_idx}/" + "/".join(str(p) for p in path)
            out[address] = container[key]
    return out


def apply_repairs(doc: Dict[str, Any], feature: str, repairs: Dict[str, int]) -> int:
    """Force the recorded plaintext values back into *doc*. Returns count applied."""
    if not repairs:
        return 0
    applied = 0
    for stream_idx, frame_idx, codec, payload in iter_frames(doc, feature):
        for container, key, path, _domain in frame_slots(feature, payload, codec):
            address = f"{stream_idx}/{frame_idx}/" + "/".join(str(p) for p in path)
            if address in repairs:
                container[key] = repairs[address]
                applied += 1
    return applied
