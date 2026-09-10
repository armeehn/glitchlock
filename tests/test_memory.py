"""Lock memory must scale with the FFedit document, not with slot count.

A 4K carrier of 312 frames exports an 85 MB motion-vector document. The
address-keyed snapshots `_run_layer` used to build for it (three dicts of
20 million string-addressed entries, plus a second full document) pushed the
process past 2.4 GB and the web service was OOM-killed mid-lock. This pins
the ratio between peak RSS and document size so it cannot creep back.
"""

import os
import resource
import subprocess
import sys

import pytest

from glitchlock import ffg

pytestmark = pytest.mark.skipif(not ffg.available(), reason="FFglitch not installed")

# Measured: json.load alone costs ~15 bytes of heap per document byte, and
# the old address-keyed snapshots ~115. The budget sits between the two.
HEAP_BYTES_PER_DOC_BYTE = 40
INTERPRETER_BASELINE = 100 * 1024 * 1024


def _lock_in_subprocess(carrier, out):
    code = (
        "from glitchlock.core import lock;"
        f"lock({carrier!r}, {out!r}, bytes(32), bytes(16), ['mv'])"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024


def test_lock_peak_rss_is_bounded_by_document_size(tmp_path):
    src = str(tmp_path / "src.mp4")
    carrier = str(tmp_path / "carrier.mpg")
    doc = str(tmp_path / "mv.json")
    subprocess.run(
        [ffg.ffgac_path(), "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=1920x1080:rate=25:duration=3",
         "-pix_fmt", "yuv420p", src],
        check=True, capture_output=True,
    )
    ffg.transcode(src, carrier, codec="mpeg2video")
    ffg.export(carrier, "mv", doc)

    peak = _lock_in_subprocess(carrier, str(tmp_path / "locked.mpg"))

    budget = os.path.getsize(doc) * HEAP_BYTES_PER_DOC_BYTE + INTERPRETER_BASELINE
    assert peak < budget, f"peak RSS {peak >> 20} MiB exceeds budget {budget >> 20} MiB"
