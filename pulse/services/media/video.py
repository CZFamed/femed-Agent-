"""视频抽帧（所有者：root）。

**为什么需要这一步**：视觉接口不支持视频输入。实测（2026-09-11）
``input_video`` 会被直接拒绝（``unknown variant input_video``），
把视频字节塞进 ``input_image`` 也报 "unsupported image"。
所以视频必须先抽成若干张静帧，再当多图送给模型。

**为什么用 ffmpeg**：解 H.264 必须有解码器，纯 Python 做不到；本机原本没有任何 ffmpeg。
依赖 ``imageio-ffmpeg``（自带静态 ffmpeg 二进制），通过子进程抽帧，比自行解码可靠得多。

**token 实测**（同一段 1.9 秒、1920×1440 的实拍视频）：

| 抽帧配置 | 帧合计 | 输入 token |
| --- | --- | --- |
| 1 帧 @1280px | 132 KB | 791 |
| 4 帧 @1280px | 528 KB | 3,008 |
| **4 帧 @768px** | 261 KB | **1,184** |
| 6 帧 @768px | 393 KB | 1,750 |

服务端按像素量折算 token，所以缩到 768px 后**4 帧的成本与一张原图（约 1,170）基本持平**。
默认取 4 帧 @768px：既看得出工序与动作，又不显著增加开销。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 实拍视频后缀
VIDEO_SUFFIXES: tuple[str, ...] = (".mp4", ".mov", ".avi", ".mkv", ".webm")

#: 默认抽帧数：4 帧足以看出"在做什么工序"
DEFAULT_FRAME_COUNT = 4
#: 默认抽帧宽度：768px 是实测的性价比拐点（4 帧 ≈ 一张原图的 token）
DEFAULT_FRAME_WIDTH = 768
#: JPEG 质量（ffmpeg -q:v，2 最好 31 最差）
DEFAULT_JPEG_QUALITY = 4
#: 单帧抽帧超时（秒）
FRAME_TIMEOUT_S = 60


class VideoFramesError(RuntimeError):
    """抽帧失败（缺 ffmpeg、格式不支持、文件损坏等），带中文原因。"""


def is_video_name(name: str) -> bool:
    return Path(str(name or "")).suffix.lower() in VIDEO_SUFFIXES


@dataclass(frozen=True)
class VideoInfo:
    duration_s: float
    width: int
    height: int
    fps: float | None = None

    @property
    def size_text(self) -> str:
        return f"{self.width}×{self.height}" if self.width and self.height else "分辨率未知"


def ffmpeg_executable() -> str | None:
    """按优先级找 ffmpeg：环境变量 → imageio-ffmpeg 自带 → PATH。

    不能只依赖 ``imageio_ffmpeg.get_ffmpeg_exe()``：它内部靠"试跑一次
    ``ffmpeg -version``"来判定二进制是否可用，而那次探测会被杀软/沙箱偶然拦住；
    一旦失败，它带 ``lru_cache`` 会把结果记一整个进程，
    于是整个控制台进程都以为"没有 ffmpeg"、视频再也入不了库（2026-09-20 实测）。
    所以这里额外**按路径**找一遍自带二进制，不受那次探测影响。
    """
    override = os.environ.get("PULSE_FFMPEG")
    if override and Path(override).is_file():
        return override
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 没装依赖不是致命错误，还要退回 PATH
        pass
    bundled = _bundled_ffmpeg()
    if bundled:
        return bundled
    return shutil.which("ffmpeg")


def _bundled_ffmpeg() -> str | None:
    """直接在 imageio-ffmpeg 的 binaries 目录里找自带二进制（不做试跑探测）。"""
    try:
        import imageio_ffmpeg
    except Exception:  # noqa: BLE001
        return None
    module_dir = Path(getattr(imageio_ffmpeg, "__file__", "") or "").parent
    binary_dir = module_dir / "binaries"
    if not binary_dir.is_dir():
        return None
    candidates = sorted(binary_dir.glob("ffmpeg-*")) + sorted(binary_dir.glob("ffmpeg.exe"))
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _require_ffmpeg() -> str:
    exe = ffmpeg_executable()
    if not exe:
        raise VideoFramesError(
            "缺少 ffmpeg，无法从视频抽帧。请安装依赖 imageio-ffmpeg（已随本项目提供），"
            "或用环境变量 PULSE_FFMPEG 指定 ffmpeg 路径。"
        )
    return exe


def probe_video(source: str | Path) -> VideoInfo | None:
    """读取时长与分辨率；失败返回 None（探测失败不该阻断流程）。"""
    path = Path(source)
    if not path.is_file():
        return None
    try:
        import imageio_ffmpeg

        reader = imageio_ffmpeg.read_frames(str(path))
        meta = next(reader)
        reader.close()
    except Exception:  # noqa: BLE001
        return None
    size = meta.get("size") or (0, 0)
    return VideoInfo(
        duration_s=float(meta.get("duration") or 0.0),
        width=int(size[0] or 0),
        height=int(size[1] or 0),
        fps=float(meta["fps"]) if meta.get("fps") else None,
    )


def frame_offsets(duration_s: float, count: int) -> list[float]:
    """在视频 10%–90% 区间均匀取点：掐掉常见的开头/结尾黑场。"""
    if count <= 0:
        return []
    if count == 1:
        return [max(0.0, duration_s * 0.5)]
    if duration_s <= 0:
        return [0.0] * count
    return [
        round(duration_s * (0.1 + 0.8 * index / (count - 1)), 2) for index in range(count)
    ]


def extract_frames(
    source: str | Path,
    *,
    count: int = DEFAULT_FRAME_COUNT,
    width: int = DEFAULT_FRAME_WIDTH,
    quality: int = DEFAULT_JPEG_QUALITY,
    duration_s: float | None = None,
) -> list[bytes]:
    """把视频抽成若干张 JPEG 静帧（按时间顺序）。"""
    path = Path(source)
    if not path.is_file():
        raise VideoFramesError(f"找不到视频文件：{path}")
    exe = _require_ffmpeg()
    info = probe_video(path) if duration_s is None else None
    total = duration_s if duration_s is not None else (info.duration_s if info else 0.0)
    offsets = frame_offsets(total or 0.0, count)

    frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="pulse-frames-") as tmp:
        for index, offset in enumerate(offsets):
            target = Path(tmp) / f"frame_{index:02d}.jpg"
            command = [
                exe,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(offset, 0.0):.2f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                f"scale={max(width, 64)}:-2",
                "-q:v",
                str(quality),
                "-y",
                str(target),
            ]
            try:
                completed = subprocess.run(
                    command, capture_output=True, timeout=FRAME_TIMEOUT_S, check=False
                )
            except subprocess.TimeoutExpired as exc:
                raise VideoFramesError(
                    f"抽帧超时（第 {index + 1} 帧，位置 {offset:.1f} 秒）"
                ) from exc
            if completed.returncode != 0 or not target.is_file():
                detail = (completed.stderr or b"").decode("utf-8", "replace").strip()[:200]
                raise VideoFramesError(
                    f"抽帧失败（第 {index + 1} 帧）：{detail or 'ffmpeg 返回非零'}"
                )
            frames.append(target.read_bytes())
    if not frames:
        raise VideoFramesError("没有抽到任何帧")
    return frames


def extract_frames_from_bytes(
    data: bytes,
    file_name: str,
    *,
    count: int = DEFAULT_FRAME_COUNT,
    width: int = DEFAULT_FRAME_WIDTH,
) -> tuple[list[bytes], VideoInfo | None]:
    """上传上来的是字节流，先落到临时文件再抽帧。"""
    suffix = Path(str(file_name or "")).suffix.lower() or ".mp4"
    with tempfile.TemporaryDirectory(prefix="pulse-video-") as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(data)
        info = probe_video(path)
        frames = extract_frames(
            path,
            count=count,
            width=width,
            duration_s=info.duration_s if info else None,
        )
    return frames, info
