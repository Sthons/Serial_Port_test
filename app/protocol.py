"""串口通讯帧格式定义、组帧、插槽解析与流式解析。

默认帧结构：
    主机指令：FA FB FC FD 00 00 FE FF
    期望响应：FA FB FC FD 00 {COM} FE FF

插槽格式约定：
    {COM} 表示该通道“期望 COM 号”对应的 1 个字节，必须作为独立字节书写，
    例如：FA FB FC FD 00 {COM} FE FF。
    期望响应必须包含且仅包含一个 {COM} 插槽。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

FRAME_HEADER = b"\xFA\xFB\xFC\xFD\x00"
FRAME_TAIL = b"\xFE\xFF"
FRAME_LENGTH = len(FRAME_HEADER) + 1 + len(FRAME_TAIL)  # 8 字节

# 主机下发的默认测试指令，COM 号位置固定为 0x00
REQUEST_FRAME = FRAME_HEADER + b"\x00" + FRAME_TAIL

COM_PLACEHOLDER = "{COM}"

# 期望响应的默认模板，None 表示 {COM} 插槽
DEFAULT_RESPONSE_TEMPLATE: tuple[int | None, ...] = (
    *FRAME_HEADER,
    None,
    *FRAME_TAIL,
)


def build_request_frame() -> bytes:
    """返回默认主机测试指令：FA FB FC FD 00 00 FE FF。"""
    return REQUEST_FRAME


def build_response_frame(com_no: int) -> bytes:
    """按默认模板和 COM 号构造响应帧：FA FB FC FD 00 <COM号> FE FF。"""
    return render_response(DEFAULT_RESPONSE_TEMPLATE, com_no)


def parse_hex_bytes(text: str) -> bytes:
    """把十六进制文本解析为字节，允许空格、逗号、冒号、短横线分隔和 0x 前缀。"""
    cleaned = re.sub(r"0[xX]", "", text.strip())
    cleaned = re.sub(r"[\s,;:_\-]+", "", cleaned)
    if not cleaned:
        raise ValueError("十六进制内容不能为空")
    if any(ch not in "0123456789abcdefABCDEF" for ch in cleaned):
        raise ValueError("包含非十六进制字符")
    if len(cleaned) % 2:
        raise ValueError("十六进制字节不完整（字符数应为偶数）")
    return bytes.fromhex(cleaned)


def parse_response_pattern(text: str) -> tuple[int | None, ...]:
    """解析带 {COM} 插槽的期望响应模板。

    每个空格/逗号分隔的片段要么是十六进制字节（可以是多个字节），
    要么是独立的 {COM} 插槽；模板必须包含且仅包含一个插槽。
    """
    tokens = [token for token in re.split(r"[\s,;]+", text.strip()) if token]
    if not tokens:
        raise ValueError("期望响应不能为空")

    segments: list[int | None] = []
    slot_count = 0
    for token in tokens:
        if token.upper() == COM_PLACEHOLDER:
            slot_count += 1
            segments.append(None)
            continue
        try:
            segments.extend(parse_hex_bytes(token))
        except ValueError as exc:
            raise ValueError(f"期望响应格式错误（{token}）：{exc}") from None

    if slot_count == 0:
        raise ValueError("期望响应必须包含一个 {COM} 插槽")
    if slot_count > 1:
        raise ValueError("期望响应只能包含一个 {COM} 插槽")
    return tuple(segments)


def render_response(template: Sequence[int | None], com_no: int) -> bytes:
    """把模板中的 {COM} 插槽替换为指定 COM 号，生成期望帧字节。"""
    if not 0 <= com_no <= 0xFF:
        raise ValueError(f"COM 号必须在 0x00~0xFF 之间：{com_no}")
    return bytes(com_no if segment is None else segment for segment in template)


def format_response_pattern(template: Sequence[int | None]) -> str:
    """把模板格式化为可编辑文本，如 FA FB FC FD 00 {COM} FE FF。"""
    return " ".join(
        COM_PLACEHOLDER if segment is None else f"{segment:02X}"
        for segment in template
    )


def parse_com_no(frame: bytes) -> int:
    """按默认帧结构校验响应帧并提取其中的 COM 号（保留给兼容用例）。"""
    if (
        len(frame) != FRAME_LENGTH
        or not frame.startswith(FRAME_HEADER)
        or not frame.endswith(FRAME_TAIL)
    ):
        raise ValueError(f"非法响应帧：{format_hex(frame)}")
    return frame[len(FRAME_HEADER)]


def format_hex(data: bytes) -> str:
    """将字节序列格式化为大写十六进制，如 "FA FB FC FD 00 01 FE FF"。"""
    return " ".join(f"{byte:02X}" for byte in data)


class FrameParser:
    """按期望响应模板从字节流中提取帧，可处理粘包、拆包与前置噪声。

    template 中固定字节需完全匹配，None 表示通配 1 个字节（{COM} 插槽）。
    """

    def __init__(
        self,
        template: Sequence[int | None] | None = None,
        max_buffer: int = 4096,
    ) -> None:
        self._template: tuple[int | None, ...] = tuple(
            DEFAULT_RESPONSE_TEMPLATE if template is None else template
        )
        if not self._template:
            raise ValueError("期望响应模板不能为空")
        for segment in self._template:
            if segment is not None and not 0 <= segment <= 0xFF:
                raise ValueError(f"期望响应模板含非法字节：{segment}")
        self._frame_length = len(self._template)
        self._buffer = bytearray()
        self._max_buffer = max_buffer

    @property
    def buffer(self) -> bytes:
        """尚未消费的缓存字节，主要用于调试。"""
        return bytes(self._buffer)

    @property
    def template(self) -> tuple[int | None, ...]:
        return self._template

    def reset(self) -> None:
        self._buffer.clear()

    def _matches(self, buffer: bytes, start: int) -> bool:
        for offset, expected in enumerate(self._template):
            if expected is not None and buffer[start + offset] != expected:
                return False
        return True

    def feed(self, data: bytes) -> list[bytes]:
        """喂入新收到的字节，返回本次解析出的所有完整帧。"""
        if data:
            self._buffer.extend(data)
        if len(self._buffer) > self._max_buffer:
            del self._buffer[: len(self._buffer) - self._max_buffer]

        buffer = bytes(self._buffer)
        frames: list[bytes] = []
        start = 0
        frame_length = self._frame_length
        while start + frame_length <= len(buffer):
            if self._matches(buffer, start):
                frames.append(buffer[start : start + frame_length])
                start += frame_length
            else:
                start += 1

        self._buffer = bytearray(buffer[start:])
        return frames
