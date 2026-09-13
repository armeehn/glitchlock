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

#: x264 quality for an H.264 carrier; --qscale is an MPEG notion and is ignored.
H264_CRF = 23
#: B-frames per GOP in the Main-profile CAVLC carrier. Baseline would need 0.
H264_BFRAMES = 2


def _transcode_h264(src: str, dst: str, gop: int, closed_gop: bool,
                    extra: Optional[List[str]]) -> None:
    """H.264 carrier through the system ffmpeg's libx264: FFglitch's ffgac has
    no H.264 encoder. CAVLC is mandatory (the patched ffedit refuses CABAC);
    repeated SPS/PPS give every IDR a splittable start code for streaming."""
    params = [
        "cabac=0", f"bframes={H264_BFRAMES}", "repeat-headers=1",
        f"keyint={gop}", f"min-keyint={gop}",
    ]
    if closed_gop:
        params += ["scenecut=0", "open-gop=0"]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FFglitchMissing("an H.264 carrier needs ffmpeg with libx264 on PATH")
    cmd = [
        ffmpeg, "-v", "error", "-y", "-i", src, "-an",
        "-c:v", "libx264", "-profile:v", "main", "-crf", str(H264_CRF),
        "-x264-params", ":".join(params), "-f", "h264",
    ]
    if extra:
        cmd += extra
    cmd.append(dst)
    _run(cmd)


#: x265 quality for an HEVC carrier, same scale as the H.264 one.
HEVC_CRF = 23
#: B-frames per GOP in the HEVC carrier.
HEVC_BFRAMES = 2


def _transcode_hevc(src: str, dst: str, gop: int, closed_gop: bool,
                    extra: Optional[List[str]]) -> None:
    """HEVC carrier through the system ffmpeg's libx265. The patched ffedit
    re-encodes CABAC, so entropy coding is not a constraint, but wavefront
    parallel processing is refused (its per-row context saves would have to
    be replayed) and x265 turns it on by default. Repeated VPS/SPS/PPS give
    every IDR a splittable start code for streaming."""
    params = [
        "wpp=0", f"bframes={HEVC_BFRAMES}", "repeat-headers=1",
        f"keyint={gop}", f"min-keyint={gop}", "log-level=error",
    ]
    if closed_gop:
        params += ["scenecut=0", "open-gop=0"]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FFglitchMissing("an HEVC carrier needs ffmpeg with libx265 on PATH")
    cmd = [
        ffmpeg, "-v", "error", "-y", "-i", src, "-an",
        "-c:v", "libx265", "-crf", str(HEVC_CRF),
        "-x265-params", ":".join(params), "-f", "hevc",
    ]
    if extra:
        cmd += extra
    cmd.append(dst)
    _run(cmd)


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
    if codec == "h264":
        _transcode_h264(src, dst, gop, closed_gop, extra)
        return
    if codec == "hevc":
        _transcode_hevc(src, dst, gop, closed_gop, extra)
        return
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
