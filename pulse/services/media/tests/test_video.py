"""视频抽帧测试：真实解码一个 7KB 测试视频（不依赖网络与模型）。

测试素材 `assets/sample.mp4` 由 ffmpeg 生成（1 秒 320×240 测试图案），
体积仅 7KB，用于验证抽帧链路本身。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pulse.services.media.describe import VisionConfig, probe_image
from pulse.services.media.video import (
    DEFAULT_FRAME_COUNT,
    DEFAULT_FRAME_WIDTH,
    VIDEO_SUFFIXES,
    VideoFramesError,
    extract_frames,
    extract_frames_from_bytes,
    ffmpeg_executable,
    frame_offsets,
    is_video_name,
    probe_video,
)

ASSETS = Path(__file__).parent / "assets"
SAMPLE = ASSETS / "sample.mp4"


def test_sample_fixture_exists() -> None:
    assert SAMPLE.is_file(), "缺少测试视频素材"
    assert SAMPLE.stat().st_size < 50_000, "测试素材应当很小，避免仓库膨胀"


def test_ffmpeg_is_available() -> None:
    """视频识别依赖 ffmpeg；imageio-ffmpeg 已随项目安装。"""
    exe = ffmpeg_executable()
    assert exe and Path(exe).is_file()


def test_is_video_name() -> None:
    for suffix in VIDEO_SUFFIXES:
        assert is_video_name(f"clip{suffix}")
        assert is_video_name(f"clip{suffix.upper()}"), "后缀大小写都要认"
    assert not is_video_name("photo.jpg")
    assert not is_video_name("")


def test_probe_video_reads_metadata() -> None:
    info = probe_video(SAMPLE)
    assert info is not None
    assert info.width == 320 and info.height == 240
    assert 0.7 <= info.duration_s <= 1.6, f"时长读取异常：{info.duration_s}"
    assert info.size_text == "320×240"


def test_probe_video_returns_none_for_missing_or_broken(tmp_path) -> None:
    assert probe_video(tmp_path / "nope.mp4") is None
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video at all")
    assert probe_video(broken) is None


def test_frame_offsets_avoid_head_and_tail() -> None:
    offsets = frame_offsets(100.0, 4)
    assert len(offsets) == 4
    assert offsets == sorted(offsets)
    assert offsets[0] >= 10.0 and offsets[-1] <= 90.0, "掐掉开头结尾，避开黑场"
    assert frame_offsets(0.0, 3) == [0.0, 0.0, 0.0], "读不到时长时不能炸"
    assert frame_offsets(10.0, 1) == [5.0]
    assert frame_offsets(10.0, 0) == []


def test_extract_frames_returns_jpegs() -> None:
    frames = extract_frames(SAMPLE, count=DEFAULT_FRAME_COUNT, width=DEFAULT_FRAME_WIDTH)
    assert len(frames) == DEFAULT_FRAME_COUNT
    for frame in frames:
        info = probe_image(frame)
        assert info.fmt == "jpeg", "抽出来的必须是 JPEG，模型只认图片格式"
        assert info.width == DEFAULT_FRAME_WIDTH, "宽度应当被缩放到指定值"


def test_extract_frames_honours_width_and_count() -> None:
    frames = extract_frames(SAMPLE, count=2, width=160)
    assert len(frames) == 2
    assert probe_image(frames[0]).width == 160
    # 帧按时间顺序，内容应有变化（测试图案会滚动）
    assert frames[0] != frames[1]


def test_extract_frames_reports_missing_file(tmp_path) -> None:
    with pytest.raises(VideoFramesError, match="找不到视频文件"):
        extract_frames(tmp_path / "nope.mp4")


def test_extract_frames_reports_broken_file(tmp_path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"definitely not an mp4")
    with pytest.raises(VideoFramesError):
        extract_frames(broken, count=1)


def test_extract_frames_from_bytes_matches_file_path() -> None:
    """上传上来的是字节流，走临时文件这条路必须和直接给路径等价。"""
    data = SAMPLE.read_bytes()
    frames, info = extract_frames_from_bytes(data, "上传的视频.mp4", count=2, width=160)
    assert len(frames) == 2
    assert info is not None and info.width == 320
    assert probe_image(frames[0]).fmt == "jpeg"


def test_video_config_defaults_keep_cost_low() -> None:
    """4 帧 @768px 的输入 token（约 1,184）与一张原图基本持平，是实测的性价比拐点。"""
    config = VisionConfig()
    assert config.video_frames == 4
    assert config.video_frame_width == 768
