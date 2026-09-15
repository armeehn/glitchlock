"""MPEG-TS transport: video + audio locked inside a transport stream.

The mux/demux tests are pure and always run. The round trips need ffmpeg and
the patched FFglitch and skip without them.
"""

import json
import shutil
import subprocess

import pytest

from glitchlock import cli, ffg, transport
from glitchlock.core import LockError
from glitchlock.stream import StreamSession
from glitchlock.transport import (
    AudioSpec,
    Program,
    Track,
    TransportSession,
    Unit,
    crc32_mpeg,
    demux,
    encode_pts,
    mux,
    run_transport,
)

KEY = bytes(range(32))
NONCE = bytes(range(16))
PACKET = transport.PACKET_SIZE


# ------------------------------------------------------------ known answers


def test_pts_field_known_answer():
    # ISO 13818-1 2.4.3.7: '0010' + 3 bits + marker + 15 + marker + 15 + marker
    assert encode_pts(0, prefix=0b0010) == bytes.fromhex("2100010001")
    assert encode_pts(126000, prefix=0b0011) == bytes.fromhex("310007d861")


def test_crc32_mpeg_known_answer():
    assert crc32_mpeg(b"123456789") == 0x0376E6E7


# ------------------------------------------------------------- mux / demux


def _program(video_sizes, audio_sizes):
    video = Track(pid=0x100, stream_type=transport.STREAM_TYPE_H264, units=[
        Unit(pts=90000 + 3600 * i + 7200, dts=90000 + 3600 * i, data=bytes([i % 251]) * n)
        for i, n in enumerate(video_sizes)])
    audio = Track(pid=0x101, stream_type=transport.STREAM_TYPE_MPEG2_AUDIO, units=[
        Unit(pts=90000 + 2160 * i, dts=None, data=bytes([(i * 7) % 251]) * n)
        for i, n in enumerate(audio_sizes)])
    return Program(tracks=[video, audio])


def test_mux_demux_round_trip_keeps_units_and_timestamps():
    # sizes straddle every packet boundary case: short, exactly one payload,
    # one byte over, and long enough to need stuffing at the tail
    prog = _program([10, 184 - 19, 184 - 18, 5000, 1], [576, 576, 1, 2000])
    ts = mux(prog)
    assert len(ts) % PACKET == 0
    assert all(ts[i] == transport.SYNC_BYTE for i in range(0, len(ts), PACKET))

    back = demux(ts)
    assert back.video.codec == "h264" and back.audio.codec == "mp2"
    assert back.video.pid == 0x100 and back.audio.pid == 0x101
    assert back.video.units == prog.video.units
    assert back.audio.units == prog.audio.units


def test_mux_is_deterministic_and_pat_pmt_lead():
    prog = _program([100, 100], [50])
    ts = mux(prog)
    assert ts == mux(prog)
    pids = [((ts[i + 1] & 0x1F) << 8) | ts[i + 2] for i in range(0, len(ts), PACKET)]
    assert pids[:2] == [transport.PID_PAT, transport.PID_PMT]


def test_demux_rejects_non_ts():
    with pytest.raises(ValueError):
        demux(b"\x00\x00\x01\xb3" + bytes(400))


def test_session_round_trips_through_json(tmp_path):
    video = StreamSession(codec="h264", nonce=NONCE.hex(), features=["mv"], gops=2)
    session = TransportSession(video=video, audio=AudioSpec(features=["samples"]))
    path = str(tmp_path / "s.json")
    session.save(path)
    loaded = TransportSession.load(path)
    assert loaded == session
    assert json.load(open(path))["format"] == transport.SESSION_FORMAT


# ------------------------------------------------------------- round trips


def _ffmpeg_ok():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def _source(d):
    src = str(d / "src.mp4")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:rate=25:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", src],
        check=True, capture_output=True)
    return src


def _carrier(tmp_path_factory, codec):
    if not ffg.available() or not _ffmpeg_ok():
        pytest.skip("FFglitch or ffmpeg not installed")
    d = tmp_path_factory.mktemp(f"ts-{codec}")
    out = str(d / "carrier.ts")
    ffg.transcode(_source(d), out, codec=codec, gop=12, closed_gop=True, container="ts")
    return out


@pytest.fixture(scope="module")
def carrier(tmp_path_factory):
    return _carrier(tmp_path_factory, "h264")


@pytest.fixture(scope="module")
def hevc_carrier(tmp_path_factory):
    return _carrier(tmp_path_factory, "hevc")


def _probe_streams(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    # ffprobe lists a stream under its program and again at top level
    return list(dict.fromkeys(r.stdout.split()))


def _probe_packets(path, kind):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries",
         "packet=pts,dts", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True)
    return r.stdout.split()


def _decodes_clean(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stderr == ""


def _extract(path, kind, fmt, out):
    """Elementary stream as stock ffmpeg sees it: the receiver's view."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-map", f"0:{kind}",
                    "-c", "copy", "-f", fmt, out], check=True, capture_output=True)
    return open(out, "rb").read()


def _session(codec, gops=1):
    return TransportSession(
        video=StreamSession(codec=codec, nonce=NONCE.hex(), features=["mv"], gops=gops),
        audio=AudioSpec(features=["samples", "scalefactors"]))


def test_carrier_is_h264_plus_mp2_in_ts(carrier):
    assert _probe_streams(carrier) == ["h264", "mp2"]
    prog = demux(open(carrier, "rb").read())
    assert prog.video.codec == "h264" and prog.audio.codec == "mp2"
    assert len(prog.video.units) == 75
    assert all(u.data.startswith(transport.AUD_H264) for u in prog.video.units)


def test_lock_keeps_timestamps_and_stays_playable(carrier, tmp_path):
    original = open(carrier, "rb").read()
    locked, stats = run_transport(original, _session("h264"), KEY, forward=True)
    assert stats.video_segments == 7 and stats.audio_frames > 100
    assert stats.slots_touched > 5000

    before, after = demux(original), demux(locked)
    assert b"".join(u.data for u in after.video.units) != b"".join(u.data for u in before.video.units)
    assert b"".join(u.data for u in after.audio.units) != b"".join(u.data for u in before.audio.units)
    assert [(u.pts, u.dts) for u in after.video.units] == [(u.pts, u.dts) for u in before.video.units]

    path = str(tmp_path / "locked.ts")
    open(path, "wb").write(locked)
    assert _probe_streams(path) == ["h264", "mp2"]
    assert _probe_packets(path, "v") == _probe_packets(carrier, "v")
    assert _probe_packets(path, "a") == _probe_packets(carrier, "a")
    assert _decodes_clean(path)


def test_unlock_restores_both_elementary_streams(carrier, tmp_path):
    original = open(carrier, "rb").read()
    session = _session("h264", gops=2)
    locked, _ = run_transport(original, session, KEY, forward=True)
    restored, _ = run_transport(locked, session, KEY, forward=False)

    back, ref = demux(restored), demux(original)
    assert back.video.units == ref.video.units
    assert back.audio.units == ref.audio.units

    path = str(tmp_path / "restored.ts")
    open(path, "wb").write(restored)
    for kind, fmt in (("v", "h264"), ("a", "mp2")):
        mine = _extract(path, kind, fmt, str(tmp_path / f"r.{fmt}"))
        theirs = _extract(carrier, kind, fmt, str(tmp_path / f"c.{fmt}"))
        assert mine == theirs, f"{fmt} elementary stream differs"


def test_wrong_key_does_not_restore(carrier):
    original = open(carrier, "rb").read()
    session = _session("h264")
    locked, _ = run_transport(original, session, KEY, forward=True)
    wrong, _ = run_transport(locked, session, bytes(32), forward=False)
    assert demux(wrong).video.units != demux(original).video.units


def test_hevc_round_trip(hevc_carrier, tmp_path):
    original = open(hevc_carrier, "rb").read()
    session = _session("hevc")
    locked, stats = run_transport(original, session, KEY, forward=True)
    assert stats.slots_touched > 1000
    path = str(tmp_path / "locked.ts")
    open(path, "wb").write(locked)
    assert _probe_streams(path) == ["hevc", "mp2"]
    assert _decodes_clean(path)
    restored, _ = run_transport(locked, session, KEY, forward=False)
    assert demux(restored).video.units == demux(original).video.units


def test_refuses_video_without_access_unit_delimiters(tmp_path):
    if not _ffmpeg_ok():
        pytest.skip("ffmpeg not installed")
    path = str(tmp_path / "m2v.ts")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=160x120:rate=25:duration=1",
         "-c:v", "mpeg2video", "-f", "mpegts", path],
        check=True, capture_output=True)
    with pytest.raises(LockError, match="h264, hevc"):
        run_transport(open(path, "rb").read(), _session("mpeg2video"), KEY, forward=True)


def test_cli_ts_lock_and_unlock(carrier, tmp_path):
    key = str(tmp_path / "k")
    open(key, "wb").write(KEY)
    locked, session = str(tmp_path / "l.ts"), str(tmp_path / "s.json")
    restored = str(tmp_path / "r.ts")
    assert cli.main(["ts-lock", carrier, "-o", locked, "--session", session,
                     "--key-file", key]) == 0
    assert _decodes_clean(locked)
    assert cli.main(["ts-unlock", locked, "-o", restored, "--session", session,
                     "--key-file", key]) == 0
    assert demux(open(restored, "rb").read()) == demux(open(carrier, "rb").read())


def test_cli_prepare_ts(tmp_path):
    if not ffg.available() or not _ffmpeg_ok():
        pytest.skip("FFglitch or ffmpeg not installed")
    out = str(tmp_path / "c.ts")
    assert cli.main(["prepare", _source(tmp_path), "-o", out, "--codec", "h264",
                     "--closed-gop", "--container", "ts"]) == 0
    assert _probe_streams(out) == ["h264", "mp2"]
