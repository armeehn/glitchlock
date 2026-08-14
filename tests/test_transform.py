"""Unit tests for the transform. These need no FFglitch and no video."""

import copy

import pytest

from glitchlock.crypto import KeyStream, invert_permutation
from glitchlock.domains import (
    DomainViolation,
    UnsupportedCodec,
    check_in_domain,
    frame_slots,
    mv_width_shift,
)
from glitchlock.transform import MODES, collect_values, transform_document

KEY = bytes(range(32))
NONCE = bytes(range(16))


def make_mv_doc(fcode=(1, 1), rows=4, cols=5, frames=3, seed=0, codec="mpeg2video"):
    """Synthesise an FFedit-shaped mv document with in-domain values."""
    import random

    rng = random.Random(seed)
    shift = mv_width_shift(codec)
    # one half-range per axis: MPEG-2 carries an independent f_code for each
    halves = [1 << (fcode[min(ax, len(fcode) - 1)] + shift - 1) for ax in (0, 1)]
    out_frames = []
    for _ in range(frames):
        grid = [
            [[rng.randrange(-halves[0], halves[0]), rng.randrange(-halves[1], halves[1])]
             for _ in range(cols)]
            for _ in range(rows)
        ]
        out_frames.append(
            {"pkt_pos": 0, "mv": {"forward": grid, "fcode": list(fcode), "overflow": "warn"}}
        )
    return {"streams": [{"codec": codec, "frames": out_frames}]}


def make_qscale_doc(frames=3, slices=4, mbs=6, seed=1):
    import random

    rng = random.Random(seed)
    out_frames = []
    for _ in range(frames):
        out_frames.append(
            {
                "qscale": {
                    "slice": [
                        {str(mb): rng.randint(1, 31) for mb in range(mbs)}
                        for _ in range(slices)
                    ]
                }
            }
        )
    return {"streams": [{"codec": "mpeg2video", "frames": out_frames}]}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("intensity", [1.0, 0.5, 0.1])
def test_mv_roundtrip(mode, intensity):
    original = make_mv_doc()
    doc = copy.deepcopy(original)

    transform_document(doc, "mv", KEY, NONCE, mode=mode, intensity=intensity, forward=True)
    assert doc != original, "locking should change something"

    transform_document(doc, "mv", KEY, NONCE, mode=mode, intensity=intensity, forward=False)
    assert doc == original


@pytest.mark.parametrize("mode", MODES)
def test_qscale_roundtrip(mode):
    original = make_qscale_doc()
    doc = copy.deepcopy(original)
    transform_document(doc, "qscale", KEY, NONCE, mode=mode, forward=True)
    assert doc != original
    transform_document(doc, "qscale", KEY, NONCE, mode=mode, forward=False)
    assert doc == original


@pytest.mark.parametrize("codec", ["mpeg2video", "mpeg4"])
def test_values_stay_in_domain(codec):
    doc = make_mv_doc(fcode=(3, 1) if codec == "mpeg2video" else (3,), codec=codec)
    transform_document(doc, "mv", KEY, NONCE, forward=True)
    for frame in doc["streams"][0]["frames"]:
        for container, key, _path, domain in frame_slots("mv", frame["mv"], codec):
            assert check_in_domain(container[key], domain)


@pytest.mark.parametrize("codec", ["mpeg2video", "mpeg4"])
def test_roundtrip_per_codec(codec):
    original = make_mv_doc(fcode=(2,) if codec == "mpeg4" else (2, 2), codec=codec)
    doc = copy.deepcopy(original)
    transform_document(doc, "mv", KEY, NONCE, forward=True)
    assert doc != original
    transform_document(doc, "mv", KEY, NONCE, forward=False)
    assert doc == original


def test_mpeg4_domain_is_one_bit_wider_than_mpeg2():
    """Regression: MPEG-4 sign-extends to 5+f_code bits, MPEG-2 to 4+f_code.

    Assuming the MPEG-2 width for an MPEG-4 stream silently wraps legal values
    such as 17 (f_code=1) down to -15 and loses them for good.
    """
    assert mv_width_shift("mpeg2video") == 4
    assert mv_width_shift("mpeg4") == 5

    mpeg2 = make_mv_doc(fcode=(1, 1), rows=1, cols=1, frames=1, codec="mpeg2video")
    mpeg4 = make_mv_doc(fcode=(1,), rows=1, cols=1, frames=1, codec="mpeg4")
    d2 = frame_slots("mv", mpeg2["streams"][0]["frames"][0]["mv"], "mpeg2video")[0][3]
    d4 = frame_slots("mv", mpeg4["streams"][0]["frames"][0]["mv"], "mpeg4")[0][3]
    assert d2 == (-16, 32)
    assert d4 == (-32, 64)


def test_out_of_domain_plaintext_is_refused():
    """A value the codec table cannot represent must stop the lock, not wrap."""
    doc = make_mv_doc(fcode=(1, 1), rows=1, cols=1, frames=1, codec="mpeg2video")
    doc["streams"][0]["frames"][0]["mv"]["forward"][0][0] = [17, 0]
    with pytest.raises(DomainViolation):
        transform_document(doc, "mv", KEY, NONCE, forward=True)


def test_unknown_codec_is_refused_not_guessed():
    doc = make_mv_doc(codec="mpeg2video")
    doc["streams"][0]["codec"] = "vp8"
    with pytest.raises(UnsupportedCodec):
        transform_document(doc, "mv", KEY, NONCE, forward=True)


def test_qscale_values_stay_legal():
    doc = make_qscale_doc()
    transform_document(doc, "qscale", KEY, NONCE, forward=True)
    for address, value in collect_values(doc, "qscale").items():
        assert 1 <= value <= 31, address


def test_permutation_preserves_multiset_per_domain():
    original = make_mv_doc(fcode=(2, 2))
    doc = copy.deepcopy(original)
    transform_document(doc, "mv", KEY, NONCE, mode="permute", forward=True)
    before = sorted(collect_values(original, "mv").values())
    after = sorted(collect_values(doc, "mv").values())
    assert before == after, "a pure permutation must not invent or destroy values"


def test_mixed_fcode_axes_use_separate_domains():
    """MPEG-2 reports one f_code per axis; x and y must not share a modulus."""
    doc = make_mv_doc(fcode=(1, 5), rows=2, cols=2, frames=1)
    # values were generated for the x-axis domain, which is the narrower one
    slots = frame_slots("mv", doc["streams"][0]["frames"][0]["mv"], "mpeg2video")
    x_domains = {d for _c, _k, p, d in slots if p[-1] == 0}
    y_domains = {d for _c, _k, p, d in slots if p[-1] == 1}
    assert x_domains == {(-16, 32)}
    assert y_domains == {(-256, 512)}


def test_wrong_key_does_not_restore():
    original = make_mv_doc()
    doc = copy.deepcopy(original)
    transform_document(doc, "mv", KEY, NONCE, forward=True)
    transform_document(doc, "mv", bytes(32), NONCE, forward=False)
    assert doc != original


def test_wrong_nonce_does_not_restore():
    original = make_mv_doc()
    doc = copy.deepcopy(original)
    transform_document(doc, "mv", KEY, NONCE, forward=True)
    transform_document(doc, "mv", KEY, bytes(16), forward=False)
    assert doc != original


def test_rejects_irreversible_feature():
    from glitchlock.domains import UnsupportedFeature

    with pytest.raises(UnsupportedFeature):
        frame_slots("q_dc", {"data": [[1, 2, 3]]}, "mpeg2video")


def test_qscale_refused_on_mpeg4():
    """FFedit does not expose qscale for MPEG-4; the table must agree."""
    from glitchlock.domains import UnsupportedFeature

    with pytest.raises(UnsupportedFeature):
        frame_slots("qscale", {"slice": [{"0": 5}]}, "mpeg4")


def test_intensity_bounds():
    doc = make_mv_doc()
    with pytest.raises(ValueError):
        transform_document(doc, "mv", KEY, NONCE, intensity=0.0)
    with pytest.raises(ValueError):
        transform_document(doc, "mv", KEY, NONCE, intensity=1.5)


def test_unknown_mode_rejected():
    doc = make_mv_doc()
    with pytest.raises(ValueError):
        transform_document(doc, "mv", KEY, NONCE, mode="scramble-harder")


def test_null_cells_are_left_alone():
    doc = make_mv_doc(rows=2, cols=2, frames=1)
    doc["streams"][0]["frames"][0]["mv"]["forward"][0][0] = None
    transform_document(doc, "mv", KEY, NONCE, forward=True)
    assert doc["streams"][0]["frames"][0]["mv"]["forward"][0][0] is None


def test_fcode_itself_is_never_modified():
    doc = make_mv_doc(fcode=(3, 3))
    transform_document(doc, "mv", KEY, NONCE, forward=True)
    for frame in doc["streams"][0]["frames"]:
        assert frame["mv"]["fcode"] == [3, 3]


def test_permutation_inverse():
    ks = KeyStream(KEY, NONCE, b"test")
    perm = ks.permutation(50)
    inverse = invert_permutation(perm)
    values = list(range(50))
    shuffled = [values[p] for p in perm]
    restored = [shuffled[p] for p in inverse]
    assert restored == values
