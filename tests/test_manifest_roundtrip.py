"""The manifest MAC must survive a trip through JSON and JavaScript.

Reported from the live web UI: unlocking via "send to unlock" always failed
with "wrong key, or the manifest has been altered", using the same key that
had just locked the file. The key was right and nothing was altered -- the
manifest had passed through the browser, and JavaScript re-serialised
``"intensity": 1.0`` as ``"intensity": 1``. The MAC covers the serialisation,
so one character changed the tag.
"""

import json

import pytest

from glitchlock.manifest import Layer, Manifest

KEY = bytes(range(32))


def _manifest():
    m = Manifest(
        ffglitch="ffedit version test",
        codec="mpeg2video",
        nonce="ab" * 16,
        carrier_sha256="00" * 32,
        carrier_bytes=1234,
        locked_sha256="11" * 32,
        locked_bytes=1234,
    )
    m.layers = [
        Layer(feature="mv", mode="full", intensity=1.0, frames=48,
              slots_total=16608, slots_touched=16608, buckets=3),
        Layer(feature="qscale", mode="full", intensity=1.0, frames=50,
              slots_total=600, slots_touched=600, buckets=1),
    ]
    m.sign(KEY)
    return m


def _through_javascript(doc):
    """What `JSON.parse` then `JSON.stringify` does to a document.

    JavaScript has one number type, so a float that holds an integral value
    is re-emitted without its fractional part: 1.0 becomes 1.
    """
    if isinstance(doc, bool):
        return doc
    if isinstance(doc, float) and doc.is_integer():
        return int(doc)
    if isinstance(doc, dict):
        return {k: _through_javascript(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_through_javascript(v) for v in doc]
    return doc


def _rebuild(doc):
    """Reconstruct a Manifest the way the web server's /api/unlock does."""
    doc = dict(doc)
    layer_fields = set(Layer.__dataclass_fields__)
    manifest_fields = set(Manifest.__dataclass_fields__) - {"layers"}
    layers = [Layer(**{k: v for k, v in l.items() if k in layer_fields})
              for l in doc.pop("layers", [])]
    return Manifest(layers=layers,
                    **{k: v for k, v in doc.items() if k in manifest_fields})


def test_manifest_verifies_after_a_browser_round_trip():
    """The reported bug, end to end."""
    m = _manifest()
    assert m.verify(KEY)

    doc = _through_javascript(json.loads(json.dumps(m.to_dict())))
    assert doc["layers"][0]["intensity"] == 1, "fixture did not reproduce the JS coercion"
    assert isinstance(doc["layers"][0]["intensity"], int)

    assert _rebuild(doc).verify(KEY), (
        "manifest stopped verifying after a JSON/JavaScript round trip -- this "
        "is the 'wrong key or altered manifest' error the web UI reported"
    )


def test_plain_json_round_trip_still_verifies():
    """The CLI path -- read straight back from the file -- must keep working."""
    m = _manifest()
    assert _rebuild(json.loads(json.dumps(m.to_dict()))).verify(KEY)


def test_integer_intensity_is_normalised():
    """An intensity given as an int must canonicalise like the float."""
    a = Layer(feature="mv", mode="full", intensity=1)
    b = Layer(feature="mv", mode="full", intensity=1.0)
    assert isinstance(a.intensity, float)
    assert a.intensity == b.intensity


def test_counts_given_as_floats_are_normalised():
    """A hand-edited manifest with 48.0 frames must not change the tag."""
    layer = Layer(feature="mv", mode="full", intensity=1.0, frames=48.0,
                  slots_total=100.0, slots_touched=100.0, buckets=2.0)
    assert isinstance(layer.frames, int)
    assert isinstance(layer.slots_total, int)
    assert isinstance(layer.slots_touched, int)
    assert isinstance(layer.buckets, int)


def test_canonical_bytes_are_identical_across_both_spellings():
    m1 = _manifest()
    m2 = _rebuild(_through_javascript(json.loads(json.dumps(m1.to_dict()))))
    assert m1.canonical_bytes() == m2.canonical_bytes()


def test_existing_manifests_still_verify():
    """Backward compatibility: a tag written before this fix must still pass.

    Every manifest the tool has written holds a float intensity, so coercion
    must be a no-op for them.
    """
    m = _manifest()
    tag = m.mac
    reloaded = _rebuild(json.loads(json.dumps(m.to_dict())))
    assert reloaded.mac == tag
    assert reloaded.verify(KEY)


@pytest.mark.parametrize("intensity", [1.0, 0.5, 0.05, 0.3])
def test_fractional_intensities_survive_too(intensity):
    m = Manifest(nonce="cd" * 16)
    m.layers = [Layer(feature="mv", mode="full", intensity=intensity)]
    m.sign(KEY)
    doc = _through_javascript(json.loads(json.dumps(m.to_dict())))
    assert _rebuild(doc).verify(KEY)


def test_a_real_alteration_is_still_caught():
    """The check must not have been softened into uselessness."""
    m = _manifest()
    doc = _through_javascript(json.loads(json.dumps(m.to_dict())))
    doc["layers"][0]["intensity"] = 0.5          # a genuine change
    assert not _rebuild(doc).verify(KEY)

    doc2 = json.loads(json.dumps(m.to_dict()))
    doc2["nonce"] = "ff" * 16
    assert not _rebuild(doc2).verify(KEY)

    doc3 = json.loads(json.dumps(m.to_dict()))
    doc3["layers"][0]["slots_touched"] = 999
    assert not _rebuild(doc3).verify(KEY)


def test_wrong_key_is_still_rejected():
    m = _manifest()
    assert not m.verify(b"\xff" * 32)
