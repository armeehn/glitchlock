"""Thin wrappers around the FFglitch binaries.

glitchlock shells out to ``ffedit`` and ``ffgac`` rather than linking anything:
FFglitch ships prebuilt binaries and is GPL, and keeping it at arm's length
means glitchlock itself can stay MIT and can track new FFglitch releases
without changes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional


class FFglitchError(RuntimeError):
    pass


class FFglitchMissing(FFglitchError):
    pass


def _resolve(name: str, override: Optional[str] = None) -> str:
    if override:
        if not os.path.isfile(override) or not os.access(override, os.X_OK):
            raise FFglitchMissing(f"{override!r} is not an executable file")
        return override
    env = os.environ.get(f"GLITCHLOCK_{name.upper()}")
    if env:
        return _resolve(name, env)
    home = os.environ.get("GLITCHLOCK_FFGLITCH_HOME")
    if home:
        candidate = os.path.join(home, name)
        if os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name)
    if not found:
        raise FFglitchMissing(
            f"could not find {name!r} on PATH. Install FFglitch from "
            "https://ffglitch.org/download/ and either add it to PATH or set "
            "GLITCHLOCK_FFGLITCH_HOME to the directory holding the binaries."
        )
    return found


def ffedit_path(override: Optional[str] = None) -> str:
    return _resolve("ffedit", override)


def ffgac_path(override: Optional[str] = None) -> str:
    return _resolve("ffgac", override)


def available() -> bool:
    try:
        ffedit_path()
        return True
    except FFglitchMissing:
        return False


def _run(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFglitchError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc.stderr


def version() -> str:
    proc = subprocess.run(
        [ffedit_path(), "-version"], capture_output=True, text=True
    )
    for line in (proc.stdout + proc.stderr).splitlines():
        if "ffedit version" in line:
            return line.strip()
    return "unknown"


def supported_features(path: str) -> List[str]:
    """Ask FFedit which features it can edit for this file."""
    proc = subprocess.run(
        [ffedit_path(), "-i", path], capture_output=True, text=True
    )
    feats: List[str] = []
    for line in (proc.stdout + proc.stderr).splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and "]:" in stripped:
            name = stripped[1:stripped.index("]")].strip()
            if name:
                feats.append(name)
    return feats


def codec_name(path: str) -> str:
    proc = subprocess.run(
        [ffedit_path(), "-i", path], capture_output=True, text=True
    )
    for line in (proc.stdout + proc.stderr).splitlines():
        stripped = line.strip()
        if stripped.startswith("Stream #") and "Video:" in stripped:
            after = stripped.split("Video:", 1)[1].strip()
            return after.split()[0].strip(",")
    return "unknown"


def export(path: str, feature: str, out_json: str) -> Dict[str, Any]:
    _run([ffedit_path(), "-v", "error", "-i", path, "-f", feature, "-e", out_json])
    with open(out_json, "r") as fh:
        return json.load(fh)


def apply(path: str, feature: str, in_json: str, out_path: str) -> None:
    _run([
        ffedit_path(), "-v", "error", "-y",
        "-i", path, "-f", feature, "-a", in_json, "-o", out_path,
    ])


def write_json(doc: Dict[str, Any], path: str) -> None:
    with open(path, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))


DEFAULT_TRANSCODE_FLAGS = ["-mpv_flags", "+nopimb+forcemv"]


def transcode(
    src: str,
    dst: str,
    codec: str = "mpeg2video",
    qscale: int = 6,
    gop: int = 25,
    extra: Optional[List[str]] = None,
    closed_gop: bool = False,
) -> None:
    """Produce a glitchable carrier elementary stream with ffgac.

    ``+nopimb+forcemv`` is the standard FFglitch encoding recipe: it stops the
    encoder from emitting "skip" macroblocks, so every macroblock carries a
    real motion vector and there is something to scramble everywhere.

    ``closed_gop`` makes every GOP self-contained and pins the GOP length, which
    is what lets a stream be cut into independently lockable segments. See
    :mod:`glitchlock.stream`.
    """
    cmd = [
        ffgac_path(), "-v", "error", "-y", "-i", src, "-an",
        *DEFAULT_TRANSCODE_FLAGS,
        "-qscale:v", str(qscale),
        "-g", str(gop),
        "-vcodec", codec,
        "-f", "rawvideo",
    ]
    if closed_gop:
        # +cgop makes GOPs independently decodable; disabling scene-cut
        # detection keeps them a fixed length so segment size is predictable.
        cmd += ["-flags", "+cgop", "-sc_threshold", "1000000000"]
    if extra:
        cmd += extra
    cmd.append(dst)
    _run(cmd)
