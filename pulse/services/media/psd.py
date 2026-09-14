"""PSD（Photoshop 文档）解码与格式转换（所有者：root）。

为什么自己解 PSD，而不是装 Pillow：入库要求"先视觉识别、后入库"，而视觉接口只吃
PNG/JPEG 这类栅格图——``.psd`` 既不能直接送模型，也没法在浏览器里预览、更没法直接
发到社媒。装 Pillow 能解决，但会给素材管线加一个 ~90 MB 的重依赖（AGENTS.md §3：
新增第三方依赖必须先报 root）。而 PSD 的**合并图（composite）**格式很简单，
标准库足够解出像素。

支持的子集（覆盖 Photoshop 常规保存的绝大多数文件）：

- PSD（版本 1）与 PSB（版本 2；大文档，部分长度字段是 8 字节）
- 色彩模式：位图(0) / 灰度(1) / 索引色(2) / RGB(3) / CMYK(4)
- 位深：8 位与 16 位（16 位取高字节，等价于 8 位精度）
- 压缩：0 = 原始、1 = RLE(PackBits)
- 带 alpha 通道时输出带透明的 PNG（CMYK + alpha 丢 alpha）

明确不支持（抛带中文原因的 :class:`UnsupportedPsdError`，绝不悄悄给一张错图）：

- 32 位浮点位深
- ZIP / ZIP + 预测压缩（Photoshop 常规保存不会产生）
- 多通道(7) / 双色调(8) / Lab(9) 等模式

输出 PNG 是**像素级解码结果**：不裁剪、不调色、不加字（AGENTS.md §3.6 零修饰口径）。
唯一的加工是可选的整体降采样（``max_side``），用于送模型与页面预览，避免 payload 过大。
"""

from __future__ import annotations

import binascii
import zlib
from dataclasses import dataclass
from pathlib import Path

#: PSD / PSB 文件签名
PSD_SIGNATURE = b"8BPS"

#: 支持的后缀
PSD_SUFFIXES: frozenset[str] = frozenset({".psd", ".psb"})

#: 送模型 / 预览时的默认最长边（像素）
DEFAULT_MAX_SIDE = 1600

_MODE_BITMAP = 0
_MODE_GRAYSCALE = 1
_MODE_INDEXED = 2
_MODE_RGB = 3
_MODE_CMYK = 4

#: 每种色彩模式的颜色通道数
_COLOR_CHANNELS = {
    _MODE_BITMAP: 1,
    _MODE_GRAYSCALE: 1,
    _MODE_INDEXED: 1,
    _MODE_RGB: 3,
    _MODE_CMYK: 4,
}

_MODE_NAMES = {
    0: "位图",
    1: "灰度",
    2: "索引色",
    3: "RGB",
    4: "CMYK",
    7: "多通道",
    8: "双色调",
    9: "Lab",
}


class UnsupportedPsdError(ValueError):
    """PSD 超出本模块支持的子集（附中文原因）。"""


@dataclass(frozen=True)
class PsdHeader:
    """PSD 文件头（读前 26 字节即可得到）。"""

    version: int
    channels: int
    height: int
    width: int
    depth: int
    color_mode: int

    @property
    def color_mode_name(self) -> str:
        return _MODE_NAMES.get(self.color_mode, f"未知({self.color_mode})")

    @property
    def size_text(self) -> str:
        return f"{self.width}×{self.height}"


@dataclass(frozen=True)
class Raster:
    """解码出来的合并图（像素已交错排列）。"""

    width: int
    height: int
    #: PNG 色彩类型：0=灰度 2=RGB 4=灰度+alpha 6=RGBA
    color_type: int
    pixels: bytes

    @property
    def channels(self) -> int:
        return {0: 1, 2: 3, 4: 2, 6: 4}[self.color_type]


def is_psd(data: bytes) -> bool:
    """按签名判断是不是 PSD / PSB（只看前 4 字节）。"""
    return data[:4] == PSD_SIGNATURE


def is_psd_name(file_name: str) -> bool:
    """按后缀判断（控制台/导出用来决定要不要解码）。"""
    return Path(str(file_name)).suffix.lower() in PSD_SUFFIXES


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def read_header(data: bytes) -> PsdHeader:
    """解析文件头；超出支持范围时抛 :class:`UnsupportedPsdError`。"""
    if len(data) < 26:
        raise UnsupportedPsdError("文件太小，不是有效的 PSD（不足 26 字节文件头）")
    if not is_psd(data):
        raise UnsupportedPsdError("不是 PSD 文件（缺少 8BPS 签名）")
    version = _u16(data, 4)
    if version not in (1, 2):
        raise UnsupportedPsdError(f"PSD 版本 {version} 不支持（只支持 1=PSD / 2=PSB）")
    # 文件头布局（共 26 字节）：签名4 + 版本2 + 保留6 + 通道2 + 高4 + 宽4 + 位深2 + 模式2
    channels = _u16(data, 12)
    if not 1 <= channels <= 56:
        raise UnsupportedPsdError(f"通道数异常：{channels}")
    height = _u32(data, 14)
    width = _u32(data, 18)
    if width <= 0 or height <= 0:
        raise UnsupportedPsdError(f"画布尺寸异常：{width}×{height}")
    depth = _u16(data, 22)
    if depth not in (1, 8, 16, 32):
        raise UnsupportedPsdError(f"位深 {depth} 不支持（支持 1 / 8 / 16）")
    color_mode = _u16(data, 24)
    if color_mode not in _COLOR_CHANNELS:
        name = _MODE_NAMES.get(color_mode, color_mode)
        raise UnsupportedPsdError(
            f"色彩模式不支持：{name}（支持 位图/灰度/索引色/RGB/CMYK）"
        )
    if depth == 1 and color_mode != _MODE_BITMAP:
        raise UnsupportedPsdError("1 位位深只出现在位图模式，文件头不一致")
    if depth == 32:
        raise UnsupportedPsdError("32 位浮点 PSD 不支持，请另存为 8/16 位后再入库")
    return PsdHeader(
        version=version,
        channels=channels,
        width=width,
        height=height,
        depth=depth,
        color_mode=color_mode,
    )


def probe_psd(data: bytes) -> PsdHeader:
    """尺寸探测（与 ``probe_image`` 风格一致）。"""
    return read_header(data)


def _unpack_bits(row: bytes, count: int) -> bytes:
    """位图模式 1 位深解包：每字节 8 像素，最高位在前。"""
    out = bytearray(count)
    for index in range(count):
        byte = row[index >> 3] if (index >> 3) < len(row) else 0
        out[index] = 255 if byte & (0x80 >> (index & 7)) else 0
    return bytes(out)


def _unpack_rle(
    payload: bytes, start: int, compressed: int, expected: int
) -> tuple[bytes, int]:
    """解一行 PackBits（RLE）数据。

    ``compressed`` 是该行在文件里占的字节数（PSD 行长度表给的），``expected``
    是解出来应有的字节数（= 行宽 × 样本字节）。返回 (解出的字节, 新偏移)。
    """
    out = bytearray()
    index = start
    length = len(payload)
    end = min(start + compressed, length)
    while index < end:
        if index >= length:
            raise UnsupportedPsdError("RLE 数据提前结束（文件可能被截断）")
        control = payload[index]
        index += 1
        if control < 128:
            run = control + 1
            out += payload[index : index + run]
            index += run
        elif control > 128:
            run = 257 - control
            if index >= length:
                raise UnsupportedPsdError("RLE 数据提前结束（重复段缺字节）")
            out += bytes([payload[index]]) * run
            index += 1
        # control == 128 是空操作
    if len(out) < expected:
        raise UnsupportedPsdError(
            f"RLE 行数据不足：解出 {len(out)} 字节，应为 {expected} 字节"
        )
    return bytes(out[:expected]), index


def _row_bytes(header: PsdHeader) -> int:
    if header.color_mode == _MODE_BITMAP:
        return (header.width + 7) // 8
    return header.width * (2 if header.depth == 16 else 1)


def _read_planes(data: bytes, header: PsdHeader) -> list[bytes]:
    """读出每个通道的平面数据（每行 ``_row_bytes`` 字节，通道内不交错）。"""
    offset = 26
    color_data_len = _u32(data, offset)
    offset += 4 + color_data_len
    resources_len = _u32(data, offset)
    offset += 4 + resources_len
    layer_len_size = 8 if header.version == 2 else 4
    layer_len = int.from_bytes(data[offset : offset + layer_len_size], "big")
    offset += layer_len_size + layer_len

    if offset + 2 > len(data):
        raise UnsupportedPsdError("文件在图像数据之前就结束了")
    compression = _u16(data, offset)
    offset += 2
    row_bytes = _row_bytes(header)
    planes: list[bytes] = []

    if compression == 0:
        index = offset
        for _channel in range(header.channels):
            plane = bytearray()
            for _row in range(header.height):
                plane += data[index : index + row_bytes]
                index += row_bytes
            planes.append(bytes(plane))
        return planes

    if compression == 1:
        step = 4 if header.version == 2 else 2
        table_size = header.channels * header.height * step
        table = data[offset : offset + table_size]
        if len(table) < table_size:
            raise UnsupportedPsdError("RLE 行长度表不完整（文件可能被截断）")
        index = offset + table_size
        for channel in range(header.channels):
            plane = bytearray()
            for row in range(header.height):
                start = (channel * header.height + row) * step
                run_len = int.from_bytes(table[start : start + step], "big")
                chunk, index = _unpack_rle(data, index, run_len, row_bytes)
                plane += chunk
            planes.append(bytes(plane))
        return planes

    raise UnsupportedPsdError(
        f"压缩方式 {compression} 不支持（只支持 0=原始 / 1=RLE）；"
        "请在 Photoshop 里用「不压缩」或「RLE」另存后再入库"
    )


def _narrow(plane: bytes) -> bytes:
    """16 位样本取高字节（等价 8 位精度）。"""
    return plane[0::2]


def decode_composite(data: bytes) -> Raster:
    """解出合并图（Photoshop 保存时写入的整图预览）。"""
    header = read_header(data)
    has_palette = header.color_mode == _MODE_INDEXED
    palette = b""
    if has_palette:
        color_len = _u32(data, 26)
        palette = data[30 : 30 + color_len]

    planes = _read_planes(data, header)
    width, height = header.width, header.height
    color_count = _COLOR_CHANNELS[header.color_mode]
    size = width * height

    if header.depth == 1:
        stride = (width + 7) // 8
        gray = b"".join(
            _unpack_bits(planes[0][row * stride : (row + 1) * stride], width)
            for row in range(height)
        )
        return Raster(width=width, height=height, color_type=0, pixels=gray)

    if header.depth == 16:
        planes = [_narrow(plane) for plane in planes]

    has_alpha = header.channels > color_count
    mode = header.color_mode

    if mode == _MODE_GRAYSCALE:
        gray = planes[0][:size]
        if has_alpha:
            pixels = bytearray(size * 2)
            pixels[0::2] = gray
            pixels[1::2] = planes[color_count][:size]
            return Raster(width=width, height=height, color_type=4, pixels=bytes(pixels))
        return Raster(width=width, height=height, color_type=0, pixels=gray)

    if mode == _MODE_INDEXED:
        if len(palette) < 768:
            raise UnsupportedPsdError("索引色 PSD 缺少调色板数据")
        pixels = bytearray(size * 3)
        for index, value in enumerate(planes[0][:size]):
            pixels[index * 3] = palette[value]
            pixels[index * 3 + 1] = palette[256 + value]
            pixels[index * 3 + 2] = palette[512 + value]
        return Raster(width=width, height=height, color_type=2, pixels=bytes(pixels))

    if mode == _MODE_RGB:
        red, green, blue = (plane[:size] for plane in planes[:3])
        if has_alpha:
            pixels = bytearray(size * 4)
            pixels[0::4] = red
            pixels[1::4] = green
            pixels[2::4] = blue
            pixels[3::4] = planes[color_count][:size]
            return Raster(width=width, height=height, color_type=6, pixels=bytes(pixels))
        pixels = bytearray(size * 3)
        pixels[0::3] = red
        pixels[1::3] = green
        pixels[2::3] = blue
        return Raster(width=width, height=height, color_type=2, pixels=bytes(pixels))

    # CMYK：PSD 里 0 = 不着墨、255 = 满墨；无 ICC 的近似转换
    cyan, magenta, yellow, black = (plane[:size] for plane in planes[:4])
    pixels = bytearray(size * 3)
    for index in range(size):
        scale = 255 - black[index]
        pixels[index * 3] = (255 - cyan[index]) * scale // 255
        pixels[index * 3 + 1] = (255 - magenta[index]) * scale // 255
        pixels[index * 3 + 2] = (255 - yellow[index]) * scale // 255
    return Raster(width=width, height=height, color_type=2, pixels=bytes(pixels))


def _box_downscale(
    pixels: bytes, width: int, height: int, channels: int, factor: int
) -> tuple[bytes, int, int]:
    """整数倍盒式降采样（只用于送模型 / 预览，不做锐化等任何修饰）。"""
    out_width = max(1, width // factor)
    out_height = max(1, height // factor)
    block = factor * factor
    stride = width * channels
    out = bytearray(out_width * out_height * channels)
    position = 0
    for out_y in range(out_height):
        top = out_y * factor * stride
        for out_x in range(out_width):
            left = top + out_x * factor * channels
            for channel in range(channels):
                total = 0
                for delta in range(factor):
                    start = left + delta * stride + channel
                    total += sum(pixels[start : start + factor * channels : channels])
                out[position] = total // block
                position += 1
    return bytes(out), out_width, out_height


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big")
        + tag
        + payload
        + binascii.crc32(tag + payload).to_bytes(4, "big")
    )


def encode_png(raster: Raster) -> bytes:
    """把栅格编码成 PNG（标准库 zlib + crc32，无第三方依赖）。"""
    channels = raster.channels
    stride = raster.width * channels
    raw = bytearray()
    for row in range(raster.height):
        raw.append(0)  # 过滤器类型 0 = None
        raw += raster.pixels[row * stride : (row + 1) * stride]
    header = (
        raster.width.to_bytes(4, "big")
        + raster.height.to_bytes(4, "big")
        + bytes([8, raster.color_type, 0, 0, 0])
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def to_raster(data: bytes, *, max_side: int | None = DEFAULT_MAX_SIDE) -> Raster:
    """解码合并图；给了 ``max_side`` 就按整数倍盒式降采样（保持宽高比）。"""
    raster = decode_composite(data)
    if not max_side or max_side <= 0:
        return raster
    longest = max(raster.width, raster.height)
    if longest <= max_side:
        return raster
    factor = max(1, min(-(-longest // max_side), raster.width, raster.height))
    pixels, width, height = _box_downscale(
        raster.pixels, raster.width, raster.height, raster.channels, factor
    )
    return Raster(width=width, height=height, color_type=raster.color_type, pixels=pixels)


def to_png(data: bytes, *, max_side: int | None = DEFAULT_MAX_SIDE) -> bytes:
    """PSD 字节 → PNG 字节（像素级解码 + 可选降采样，不做任何修饰）。"""
    return encode_png(to_raster(data, max_side=max_side))
