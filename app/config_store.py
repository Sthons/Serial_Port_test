"""应用配置的持久化读写与校验。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .protocol import parse_hex_bytes, parse_response_pattern

CONFIG_VERSION = 1
VALID_PARITY = {"N", "O", "E", "M", "S"}
VALID_BYTESIZES = {5, 6, 7, 8}
VALID_STOPBITS = {1.0, 1.5, 2.0}


@dataclass
class ChannelSettings:
    port: str
    expected_com: int
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False


@dataclass
class AppSettings:
    request_interval: float
    response_timeout: float
    command: str
    response_pattern: str
    channels: list[ChannelSettings]
    target_rounds: int = 0


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    return float(value)


def _integer(value: object, label: str) -> int:
    number = _number(value, label)
    if int(number) != number:
        raise ValueError(f"{label} 必须是整数")
    return int(number)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} 必须是布尔值")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value.strip()


def load_settings(path: Path) -> AppSettings:
    """读取并校验 JSON 配置文件，失败时抛出 ValueError。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件不是合法 JSON：{exc}") from None
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是对象")

    version = raw.get("version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ValueError(f"不支持的配置版本：{version}")

    request_interval = _number(raw.get("request_interval"), "请求间隔")
    response_timeout = _number(raw.get("response_timeout"), "响应等待时间")
    if request_interval <= 0:
        raise ValueError("请求间隔必须大于 0")
    if response_timeout <= 0:
        raise ValueError("响应等待时间必须大于 0")
    if response_timeout > request_interval:
        raise ValueError("响应等待时间不能大于请求间隔")

    target_rounds = _integer(raw.get("target_rounds", 0), "目标轮次")
    if target_rounds < 0:
        raise ValueError("目标轮次必须大于或等于 0")

    command = _text(raw.get("command"), "主机指令")
    response_pattern = _text(raw.get("response_pattern"), "期望响应")
    try:
        parse_hex_bytes(command)
        parse_response_pattern(response_pattern)
    except ValueError as exc:
        raise ValueError(f"通讯帧配置无效：{exc}") from None

    raw_channels = raw.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ValueError("channels 必须是非空数组")

    channels: list[ChannelSettings] = []
    seen_ports: set[str] = set()
    for index, item in enumerate(raw_channels, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个通道配置必须是对象")
        port = _text(item.get("port"), f"第 {index} 个通道的串口").upper()
        if port in seen_ports:
            raise ValueError(f"串口 {port} 重复")
        seen_ports.add(port)

        expected_com = _integer(
            item.get("expected_com"), f"通道 {port} 的期望 COM 号"
        )
        if not 0 <= expected_com <= 0xFF:
            raise ValueError(f"通道 {port} 的期望 COM 号必须在 0~255 之间")
        baudrate = _integer(item.get("baudrate"), f"通道 {port} 的波特率")
        if baudrate <= 0:
            raise ValueError(f"通道 {port} 的波特率必须大于 0")
        bytesize = _integer(item.get("bytesize"), f"通道 {port} 的数据位")
        if bytesize not in VALID_BYTESIZES:
            raise ValueError(f"通道 {port} 的数据位必须是 5~8")
        parity = _text(item.get("parity"), f"通道 {port} 的校验位").upper()
        if parity not in VALID_PARITY:
            raise ValueError(f"通道 {port} 的校验位必须是 N/O/E/M/S")
        stopbits = _number(item.get("stopbits"), f"通道 {port} 的停止位")
        if stopbits not in VALID_STOPBITS:
            raise ValueError(f"通道 {port} 的停止位必须是 1、1.5 或 2")

        channels.append(
            ChannelSettings(
                port=port,
                expected_com=expected_com,
                baudrate=baudrate,
                bytesize=bytesize,
                parity=parity,
                stopbits=stopbits,
                xonxoff=_boolean(
                    item.get("xonxoff", False), f"通道 {port} 的 XON/XOFF"
                ),
                rtscts=_boolean(
                    item.get("rtscts", False), f"通道 {port} 的 RTS/CTS"
                ),
                dsrdtr=_boolean(
                    item.get("dsrdtr", False), f"通道 {port} 的 DSR/DTR"
                ),
            )
        )

    return AppSettings(
        request_interval=request_interval,
        response_timeout=response_timeout,
        command=command,
        response_pattern=response_pattern,
        channels=channels,
        target_rounds=target_rounds,
    )


def save_settings(path: Path, settings: AppSettings) -> None:
    """把配置写入 JSON 文件，自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CONFIG_VERSION, **asdict(settings)}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
