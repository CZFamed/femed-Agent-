"""PSD 适配测试：文件头、合并图解码（原始 / RLE）、色彩模式、降采样、PNG 输出。

测试里的 PSD 由本文件自己拼字节生成（标准库），不依赖任何外部样例文件。
"""

from __future__ import annotations

import struct
import zlib

import pytest

from pulse.services.media.config import ALLOWED_IMAGE_SUFFIXES, MAX_PSD_BYTES
from pulse.services.media.describe import image_for_vision, probe_image
from pulse.services.media.psd import (
    Raster,
    UnsupportedPsdError,
    decode_composite,
    encode_png,
    is_psd,
    is_psd_name,
    read_header,
    to_png,
    to_raster,
)


def _packbits(row: bytes) -> bytes:
    """最简 PackBits 编码：整行按字面量段打包（0..127 表示 +1 个字节）。"""
    out = bytearray()
    for start in range(0, len(row), 128):
        chunk = row[start : start + 128]
        out.append(len(chunk) - 1)
        out += chunk
    return bytes(out)


def build_psd(
    planes: list[bytes],
    *,
    width: int,
    height: int,
    color_mode: int = 3,
    depth: int = 8,
    compression: int = 0,
    version: int = 1,
    palette: bytes = b"",
    channels: int | None = None,
    resources: bytes = b"",
    layer_info: bytes = b"",
) -> bytes:
    """按 PSD 结构拼一份最小可解析文件（只含合并图，没有图层）。"""
    count = channels if channels is not None else len(planes)
    header = (
        b"8BPS"
        + struct.pack(">H", version)
        + b"\x00" * 6
        + struct.pack(">HIIHH", count, height, width, depth, color_mode)
    )
    color_data = struct.pack(">I", len(palette)) + palette
    resource_block = struct.pack(">I", len(resources)) + resources
    layer_size = 8 if version == 2 else 4
    layer_block = len(layer_info).to_bytes(layer_size, "big") + layer_info
    sample_bytes = 2 if depth == 16 else 1
    row_bytes = (width + 7) // 8 if color_mode == 0 else width * sample_bytes
    if compression == 0:
        body = b"".join(plane[: row_bytes * height] for plane in planes)
    else:
        step = 4 if version == 2 else 2
        table = bytearray()
        packed = bytearray()
        for plane in planes:
            for row in range(height):
                chunk = _packbits(plane[row * row_bytes : (row + 1) * row_bytes])
                table += len(chunk).to_bytes(step, "big")
                packed += chunk
        body = bytes(table) + bytes(packed)
    return header + color_data + resource_block + layer_block + struct.pack(">H", compression) + body


def parse_png(data: bytes) -> dict[str, object]:
    """只解出 PNG 的结构信息（标准库就能验，不需要图像库）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    chunks: dict[bytes, list[bytes]] = {}
    while offset < len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        tag = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        assert zlib.crc32(tag + payload) == int.from_bytes(
            data[offset + 8 + length : offset + 12 + length], "big"
        ), "PNG 分块 CRC 必须正确"
        chunks.setdefault(tag, []).append(payload)
        offset += 12 + length
    ihdr = chunks[b"IHDR"][0]
    return {
        "width": int.from_bytes(ihdr[0:4], "big"),
        "height": int.from_bytes(ihdr[4:8], "big"),
        "depth": ihdr[8],
        "color_type": ihdr[9],
        "raw": zlib.decompress(b"".join(chunks[b"IDAT"])),
        "tags": set(chunks),
    }


def rgb_planes(width: int, height: int) -> list[bytes]:
    """三通道平面：R 全 10、G 全 20、B 全 30（便于断言像素顺序）。"""
    size = width * height
    return [bytes([10]) * size, bytes([20]) * size, bytes([30]) * size]


def test_signature_and_suffix() -> None:
    assert is_psd(b"8BPS\x00\x01rest")
    assert not is_psd(b"\x89PNG\r\n\x1a\n")
    assert is_psd_name(r"设计稿\菲美得海报.PSD")
    assert is_psd_name("banner.psb")
    assert not is_psd_name("photo.jpg")
    assert ".psd" in ALLOWED_IMAGE_SUFFIXES
    assert ".psb" in ALLOWED_IMAGE_SUFFIXES, "大文档格式同样要能进库"
    assert MAX_PSD_BYTES > 20 * 1024 * 1024, "设计稿上限要比实拍图宽松"


def test_read_header_reports_size_and_mode() -> None:
    data = build_psd(rgb_planes(4, 3), width=4, height=3)
    header = read_header(data)
    assert (header.width, header.height) == (4, 3)
    assert header.color_mode_name == "RGB"
    assert header.depth == 8


def test_probe_image_reads_psd_dimensions() -> None:
    data = build_psd(rgb_planes(8, 5), width=8, height=5)
    info = probe_image(data)
    assert info.fmt == "psd"
    assert (info.width, info.height) == (8, 5)


def test_decode_rgb_raw_pixels() -> None:
    raster = decode_composite(build_psd(rgb_planes(3, 2), width=3, height=2))
    assert raster.color_type == 2
    assert raster.pixels[:6] == bytes([10, 20, 30, 10, 20, 30])
    assert len(raster.pixels) == 3 * 2 * 3


def test_realistic_sections_are_skipped() -> None:
    """真实 PSD 的图像资源段与图层段都很长，必须按长度跳过再取合并图。"""
    planes = rgb_planes(4, 2)
    data = build_psd(
        planes,
        width=4,
        height=2,
        resources=b"8BIM" + bytes(range(256)) * 4,   # 模拟图像资源块
        layer_info=bytes(range(200)) * 3,            # 模拟图层与蒙版信息
    )
    raster = decode_composite(data)
    assert raster.pixels[:3] == bytes([10, 20, 30])
    assert (raster.width, raster.height) == (4, 2)


def test_psb_header_uses_eight_byte_layer_length() -> None:
    """PSB（版本 2）的图层段长度是 8 字节，跳过长度算错就会解出乱码。"""
    data = build_psd(
        rgb_planes(2, 2),
        width=2,
        height=2,
        version=2,
        resources=b"res" * 10,
        layer_info=b"layer" * 7,
    )
    assert read_header(data).version == 2
    assert decode_composite(data).pixels[:3] == bytes([10, 20, 30])


def test_decode_rgb_rle_matches_raw() -> None:
    planes = rgb_planes(9, 4)
    raw = decode_composite(build_psd(planes, width=9, height=4, compression=0))
    rle = decode_composite(build_psd(planes, width=9, height=4, compression=1))
    assert rle.pixels == raw.pixels, "RLE 与原始压缩解出来的像素必须一致"


def test_decode_rle_handles_repeat_runs() -> None:
    """RLE 的重复段（control > 128）也要能解——上面用的编码器只产生字面量段。"""
    width, height = 40, 2
    planes = rgb_planes(width, height)
    raw = build_psd(planes, width=width, height=height, compression=0)
    # 手工把原始图像数据段（连压缩标记一起）换成 RLE：每行都是一次完整的重复段
    prefix = raw[: len(raw) - width * height * 3 - 2]
    values = [10, 20, 30]
    rows = [
        bytes([257 - width]) + bytes([value])
        for value in values
        for _ in range(height)
    ]
    table = b"".join(len(row).to_bytes(2, "big") for row in rows)
    data = prefix + struct.pack(">H", 1) + table + b"".join(rows)
    raster = decode_composite(data)
    assert raster.pixels[:3] == bytes(values)
    assert raster.pixels[3:6] == bytes(values)
    assert len(raster.pixels) == width * height * 3


def test_grayscale_alpha_keeps_transparency() -> None:
    gray = bytes([5]) * 4
    alpha = bytes([255]) * 4
    raster = decode_composite(
        build_psd([gray, alpha], width=2, height=2, color_mode=1, channels=2)
    )
    assert raster.color_type == 4
    assert raster.pixels == bytes([5, 255, 5, 255, 5, 255, 5, 255])


def test_cmyk_converted_to_rgb() -> None:
    size = 4
    planes = [
        bytes([255]) * size,  # C 满
        bytes([0]) * size,    # M 无
        bytes([0]) * size,    # Y 无
        bytes([0]) * size,    # K 无
    ]
    raster = decode_composite(build_psd(planes, width=2, height=2, color_mode=4))
    assert raster.color_type == 2
    assert raster.pixels[:3] == bytes([0, 255, 255]), "满青应转成 (0,255,255)"


def test_sixteen_bit_uses_high_byte() -> None:
    size = 2  # 1×2
    planes = [
        bytes([0x12, 0xFF, 0x34, 0x00]),
        bytes([0, 0, 0, 0]),
        bytes([0, 0, 0, 0]),
    ]
    raster = decode_composite(
        build_psd(planes, width=1, height=2, depth=16)
    )
    assert raster.pixels[:3] == bytes([0x12, 0, 0])
    assert raster.pixels[3:6] == bytes([0x34, 0, 0])


def test_indexed_uses_palette() -> None:
    palette = bytearray(768)
    # PSD 调色板是三个平面：R 0-255、G 256-511、B 512-767
    palette[0], palette[256], palette[512] = 200, 100, 50
    palette[1], palette[257], palette[513] = 210, 110, 60
    raster = decode_composite(
        build_psd([bytes([0, 1])], width=2, height=1, color_mode=2, palette=bytes(palette))
    )
    assert raster.pixels[:3] == bytes([200, 100, 50])
    assert raster.pixels[3:6] == bytes([210, 110, 60])


def test_downscale_averages_blocks() -> None:
    width = height = 4
    # 左上 2×2 块取到 0/100/0/100 → 平均 50
    plane = bytes([0, 100, 0, 100] * 2 + [0] * 8)
    raster = to_raster(
        build_psd([plane, plane, plane], width=width, height=height),
        max_side=2,
    )
    assert (raster.width, raster.height) == (2, 2)
    assert raster.pixels[0] == 50, "4 个像素取平均"


def test_png_output_is_wellformed() -> None:
    png = to_png(build_psd(rgb_planes(5, 3), width=5, height=3), max_side=None)
    info = parse_png(png)
    assert (info["width"], info["height"]) == (5, 3)
    assert info["color_type"] == 2
    assert info["depth"] == 8
    assert info["tags"] == {b"IHDR", b"IDAT", b"IEND"}
    raw = info["raw"]
    assert len(raw) == 3 * (1 + 5 * 3), "每行一个过滤器字节"
    assert raw[0] == 0 and all(raw[row * 16] == 0 for row in range(3))


def test_encode_png_rejects_unknown_color_type() -> None:
    with pytest.raises(KeyError):
        Raster(width=1, height=1, color_type=99, pixels=b"\x00").channels


def test_image_for_vision_converts_psd_to_png() -> None:
    data = build_psd(rgb_planes(4, 4), width=4, height=4)
    payload, mime = image_for_vision(data, "设计稿.psd")
    assert mime == "image/png"
    assert parse_png(payload)["width"] == 4
    # 普通图片原样放行
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 10
    payload, mime = image_for_vision(jpeg, "photo.jpg")
    assert payload == jpeg and mime == "image/jpeg"


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"not a psd at all" + b"\x00" * 20, "签名"),
        (build_psd([b"\x00" * 4], width=2, height=2, color_mode=9), "色彩模式"),
        (build_psd([b"\x00" * 16], width=2, height=2, depth=32), "32 位"),
        (build_psd(rgb_planes(2, 2), width=2, height=2, compression=2), "压缩方式"),
    ],
)
def test_unsupported_files_raise_with_chinese_reason(data: bytes, reason: str) -> None:
    with pytest.raises(UnsupportedPsdError) as excinfo:
        decode_composite(data)
    assert reason in str(excinfo.value)


def test_truncated_rle_is_reported() -> None:
    data = build_psd(rgb_planes(20, 1), width=20, height=1, compression=1)
    with pytest.raises(UnsupportedPsdError):
        decode_composite(data[:-30])


def test_probe_image_does_not_raise_on_unsupported_psd() -> None:
    """兜底描述链路不能被一个解不出来的 PSD 打断：尺寸读不到就留空。"""
    data = build_psd([b"\x00" * 4], width=2, height=2, color_mode=9)
    info = probe_image(data)
    assert info.fmt == "psd"
    assert info.width is None and info.height is None


def test_encode_png_roundtrip_pixels() -> None:
    raster = Raster(width=2, height=1, color_type=2, pixels=bytes([1, 2, 3, 4, 5, 6]))
    info = parse_png(encode_png(raster))
    assert info["raw"] == bytes([0, 1, 2, 3, 4, 5, 6])
