"""多通道并行发送、按间隔轮询、持续监听与判定。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import serial

from .protocol import (
    DEFAULT_RESPONSE_TEMPLATE,
    REQUEST_FRAME,
    FrameParser,
    format_hex,
    render_response,
)

STATUS_IDLE = "待测试"
STATUS_OPENING = "打开中"
STATUS_WAITING = "等待响应"
STATUS_PASSED = "响应正确"
STATUS_FAILED = "测试失败"
STATUS_STOPPED = "已停止"
STATUS_NOT_TESTED = "未测试"

READ_TIMEOUT = 0.05  # 单次读取阻塞时长（秒），用于及时响应停止信号
WRITE_TIMEOUT = 1.0  # 单次发送超时（秒）
SEND_BARRIER_TIMEOUT = 3.0
RX_PREVIEW_LIMIT = 64  # 每轮最多保留的原始接收字节数，用于超时诊断

EventCallback = Callable[[dict], None]


@dataclass
class PortConfig:
    """单个串口的测试参数。"""

    port: str
    baudrate: int = 115200
    expected_com: int = 0
    bytesize: int = serial.EIGHTBITS
    parity: str = serial.PARITY_NONE
    stopbits: float = serial.STOPBITS_ONE
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False


FLOW_TEXT = {
    "xonxoff": "XON/XOFF",
    "rtscts": "RTS/CTS",
    "dsrdtr": "DSR/DTR",
}


def describe_serial(config: PortConfig) -> str:
    """将串口参数格式化为日志可读文本，如 115200 8N1 / XON/XOFF+RTS/CTS。"""
    stopbits = f"{config.stopbits:g}"
    flows = [
        label
        for enabled, label in (
            (config.xonxoff, FLOW_TEXT["xonxoff"]),
            (config.rtscts, FLOW_TEXT["rtscts"]),
            (config.dsrdtr, FLOW_TEXT["dsrdtr"]),
        )
        if enabled
    ]
    flow_text = "+".join(flows) if flows else "无流控"
    return f"{config.baudrate} {config.bytesize}{config.parity}{stopbits} / {flow_text}"


@dataclass
class PortResult:
    """单个串口的测试结果。"""

    index: int
    config: PortConfig
    status: str = STATUS_IDLE
    detail: str = ""
    response: bytes | None = None
    response_count: int = 0
    error: str | None = None
    # 以下为轮询过程中的内部状态
    cycle_number: int = 0
    cycle_active: bool = False
    cycle_responded: bool = False
    cycle_rx_count: int = 0
    cycle_rx_preview: bytes = b""


@dataclass
class TestReport:
    """一轮测试的汇总结果（可包含多次轮询）。"""

    results: list[PortResult]
    request_interval: float = 1.0
    response_timeout: float = 1.0
    duration: float = 0.0
    aborted_reason: str | None = None  # 打开/发送阶段中止
    failure_reason: str | None = None  # 某轮响应错误或丢失
    failed_round: int = 0
    cycles_completed: int = 0
    rounds_attempted: int = 0
    stopped: bool = False

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.status == STATUS_PASSED)

    @property
    def passed(self) -> bool:
        """无任何失败且至少完成一轮，即视为通讯正常。"""
        return (
            self.aborted_reason is None
            and self.failure_reason is None
            and self.cycles_completed > 0
        )

    def summary_text(self) -> str:
        total = len(self.results)
        detail = "；".join(
            f"{result.config.port} {result.status}（累计 {result.response_count} 帧）"
            for result in self.results
        )
        if self.aborted_reason:
            headline = f"测试中止：{self.aborted_reason}"
        elif self.failure_reason:
            headline = f"测试失败（第 {self.failed_round} 轮）：{self.failure_reason}"
        elif self.stopped:
            headline = f"手动停止：完成 {self.cycles_completed} 轮"
        elif self.cycles_completed:
            headline = f"完成 {self.cycles_completed} 轮，全部正常"
        else:
            headline = "测试结束：未完成任何轮次"
        return f"{headline}（用时 {self.duration:.2f} 秒）｜{detail}"

    def headline_text(self) -> str:
        """仅包含总体结论，不含具体端口信息，用于广播到所有通道日志。"""
        if self.aborted_reason:
            headline = "测试中止"
        elif self.failure_reason:
            headline = f"测试失败（第 {self.failed_round} 轮）"
        elif self.stopped:
            headline = f"已手动停止：完成 {self.cycles_completed} 轮"
        elif self.cycles_completed:
            headline = f"测试正常：完成 {self.cycles_completed} 轮"
        else:
            headline = "测试结束：未完成任何轮次"
        total = len(self.results)
        return f"{headline}（{total} 个通道，用时 {self.duration:.2f} 秒）"


def _log(emit: EventCallback, level: str, message: str, index: int | None = None) -> None:
    """发送日志事件；带 index 的日志只归属对应通道，否则广播到所有通道。"""
    event: dict[str, Any] = {"type": "log", "level": level, "message": message}
    if index is not None:
        event["index"] = index
    emit(event)


def _emit_port(emit: EventCallback, result: PortResult) -> None:
    emit(
        {
            "type": "port",
            "index": result.index,
            "port": result.config.port,
            "status": result.status,
            "detail": result.detail,
            "count": result.response_count,
            "round": result.cycle_number,
            "response": result.response,
        }
    )


class SerialTester:
    """负责并行打开串口、按间隔轮询请求并持续监听响应。"""

    def __init__(self, serial_factory: Callable[..., Any] | None = None) -> None:
        # 注入串口工厂，便于单元测试替换为模拟串口
        self._serial_factory = serial_factory or serial.Serial

    def run(
        self,
        configs: Iterable[PortConfig],
        request_interval: float = 1.0,
        response_timeout: float = 1.0,
        request_frame: bytes | None = None,
        response_template: Sequence[int | None] | None = None,
        max_cycles: int | None = None,
        on_event: EventCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> TestReport:
        """立即发送一次请求，之后按间隔轮询，直到手动停止或出现失败。

        max_cycles 仅用于自动化测试限制轮数，界面运行时保持 None（无限轮询）。
        """
        emit: EventCallback = on_event or (lambda event: None)
        stop_event = stop_event or threading.Event()
        configs = list(configs)
        results = [PortResult(index=i, config=cfg) for i, cfg in enumerate(configs)]
        report = TestReport(
            results=results,
            request_interval=request_interval,
            response_timeout=response_timeout,
        )
        started_at = time.monotonic()
        opened: list[tuple[PortResult, Any]] = []
        readers: list[threading.Thread] = []
        read_stop = threading.Event()
        lock = threading.Lock()
        failure: dict[str, Any] = {"reason": None, "round": 0}
        cycle_number = 0

        def set_failure(reason: str, round_no: int | None = None) -> None:
            with lock:
                if failure["reason"] is None:
                    failure["reason"] = reason
                    failure["round"] = cycle_number if round_no is None else round_no

        def finish(level: str) -> TestReport:
            report.duration = time.monotonic() - started_at
            report.stopped = stop_event.is_set()
            report.rounds_attempted = cycle_number
            # 任何中止路径下，未开始测试的通道也要有明确结论，不能停留在“待测试”
            for result in report.results:
                if result.status != STATUS_IDLE:
                    continue
                result.status = STATUS_NOT_TESTED
                if report.aborted_reason:
                    result.detail = "测试中止，未开始测试"
                elif report.stopped:
                    result.detail = "手动停止，未开始测试"
                else:
                    result.detail = "未开始测试"
            # 端口级结论只写入各自通道，避免其他通道日志串入
            for result in report.results:
                if result.status == STATUS_PASSED:
                    result_level = "success"
                elif result.status == STATUS_FAILED:
                    result_level = "error"
                elif result.status in (STATUS_STOPPED, STATUS_NOT_TESTED):
                    result_level = "warning"
                else:
                    result_level = "info"
                if result.status == STATUS_NOT_TESTED:
                    message = "最终结果：未测试"
                else:
                    message = f"最终结果：{result.status}"
                    if result.response_count:
                        message += f"（累计 {result.response_count} 帧）"
                _log(emit, result_level, message, index=result.index)
            _log(emit, level, report.headline_text())
            emit({"type": "report", "report": report})
            return report

        if not results:
            report.aborted_reason = "未配置任何串口"
            return finish("error")

        names = [cfg.port.strip().upper() for cfg in configs]
        if len(set(names)) != len(names):
            report.aborted_reason = "串口名称重复"
            return finish("error")

        if request_interval <= 0:
            raise ValueError("请求间隔必须大于 0 秒")
        if response_timeout <= 0:
            raise ValueError("响应等待时间必须大于 0 秒")
        if response_timeout > request_interval:
            raise ValueError("响应等待时间不能大于请求间隔")

        frame_to_send = REQUEST_FRAME if request_frame is None else bytes(request_frame)
        if not frame_to_send:
            raise ValueError("主机指令不能为空")
        template = tuple(
            DEFAULT_RESPONSE_TEMPLATE
            if response_template is None
            else response_template
        )
        if not template:
            raise ValueError("期望响应不能为空")
        if sum(1 for segment in template if segment is None) != 1:
            raise ValueError("期望响应必须包含且仅包含一个 {COM} 插槽")
        for config in configs:
            if not 0 <= config.expected_com <= 0xFF:
                raise ValueError(
                    f"{config.port} 的期望 COM 号必须在 0~255 之间"
                )

        try:
            # 1. 并行打开全部串口，任意一个失败即中止
            for result in results:
                config = result.config
                result.status = STATUS_OPENING
                result.detail = "正在打开串口"
                _emit_port(emit, result)
                _log(
                    emit,
                    "info",
                    f"正在打开 {config.port}（{describe_serial(config)}）…",
                    index=result.index,
                )

            opened, open_errors = self._open_all(results, emit)
            if open_errors:
                first_index = min(open_errors)
                report.aborted_reason = (
                    f"{results[first_index].config.port} {open_errors[first_index]}"
                )
                return finish("error")
            _log(emit, "info", f"{len(opened)} 个串口已并行打开完成")

            # 2. 启动常驻监听线程
            for result, handle in opened:
                reader = threading.Thread(
                    target=self._listen_port,
                    args=(
                        result,
                        handle,
                        read_stop,
                        stop_event,
                        emit,
                        lock,
                        set_failure,
                        template,
                    ),
                    daemon=True,
                )
                reader.start()
                readers.append(reader)

            # 3. 立即发起第一轮请求，之后按间隔轮询
            try:
                while not stop_event.is_set() and failure["reason"] is None:
                    if max_cycles is not None and cycle_number >= max_cycles:
                        break
                    cycle_number += 1
                    report.rounds_attempted = cycle_number
                    with lock:
                        for result in results:
                            result.cycle_number = cycle_number
                            result.cycle_active = False
                            result.cycle_responded = False
                            result.cycle_rx_count = 0
                            result.cycle_rx_preview = b""
                            result.status = STATUS_WAITING
                            result.detail = f"第 {cycle_number} 轮等待响应"
                    # 清掉上一轮遗留的缓存，避免迟到的响应被算进本轮
                    self._flush_inputs(opened)
                    emit({"type": "cycle", "round": cycle_number})
                    for result in results:
                        _emit_port(emit, result)
                    _log(
                        emit,
                        "info",
                        f"第 {cycle_number} 轮：同时发送指令 {format_hex(frame_to_send)}",
                    )

                    cycle_started = time.monotonic()
                    with lock:
                        # 只有本轮指令发出后才开始接收，之前的帧一律视为轮间数据
                        for result in results:
                            result.cycle_active = True
                    send_errors = self._send_all(opened, frame_to_send)
                    if send_errors:
                        first_failed: PortResult | None = None
                        for result, _handle in opened:
                            message = send_errors.get(result.index)
                            if message:
                                with lock:
                                    result.status = STATUS_FAILED
                                    result.error = message
                                    result.detail = message
                                    result.cycle_active = False
                                if first_failed is None:
                                    first_failed = result
                                _emit_port(emit, result)
                                _log(
                                    emit,
                                    "error",
                                    f"{result.config.port} {message}",
                                    index=result.index,
                                )
                        if first_failed is not None:
                            set_failure(
                                f"{first_failed.config.port} 第 {cycle_number} 轮"
                                f"{send_errors[first_failed.index]}",
                                cycle_number,
                            )
                        # 不立即终止：继续等待本轮其余通道的响应，便于排查

                    deadline = cycle_started + max(response_timeout, 0.01)
                    while not stop_event.is_set() and time.monotonic() < deadline:
                        with lock:
                            resolved = all(
                                result.cycle_responded
                                or result.status == STATUS_FAILED
                                for result in results
                            )
                        if resolved:
                            break
                        stop_event.wait(0.005)
                    with lock:
                        for result in results:
                            result.cycle_active = False
                        all_responded = all(
                            result.cycle_responded for result in results
                        )

                    # 全部响应正确：即使此刻收到停止请求，也计入已完成轮次
                    if failure["reason"] is None and all_responded:
                        report.cycles_completed = cycle_number
                        _log(
                            emit,
                            "success",
                            f"第 {cycle_number} 轮：{len(results)} 个通道全部响应正确",
                        )
                        if stop_event.is_set():
                            break
                        if max_cycles is not None and cycle_number >= max_cycles:
                            break

                        # 4. 等待到下一次请求时间（间隔按本轮请求发起时刻计算）
                        next_at = cycle_started + max(request_interval, 0.01)
                        while not stop_event.is_set() and time.monotonic() < next_at:
                            stop_event.wait(
                                min(0.02, max(next_at - time.monotonic(), 0.001))
                            )
                        continue

                    if stop_event.is_set():
                        break

                    # 本轮结束：给未响应且未失败的通道补一条丢失记录，便于排查
                    unresolved = [
                        result
                        for result in results
                        if not result.cycle_responded
                        and result.status != STATUS_FAILED
                    ]
                    for result in unresolved:
                        expected_frame = render_response(
                            template, result.config.expected_com
                        )
                        with lock:
                            rx_count = result.cycle_rx_count
                            rx_preview = result.cycle_rx_preview
                            if rx_count == 0:
                                message = (
                                    f"第 {cycle_number} 轮 "
                                    f"{response_timeout:.1f} 秒内未收到任何数据"
                                    "（响应丢失）"
                                )
                            else:
                                suffix = " …" if rx_count > len(rx_preview) else ""
                                message = (
                                    f"第 {cycle_number} 轮收到 {rx_count} 字节，"
                                    "但未匹配期望响应"
                                    f"（期望 {format_hex(expected_frame)}；"
                                    f"原始数据：{format_hex(rx_preview)}{suffix}）"
                                )
                            result.status = STATUS_FAILED
                            result.error = message
                            result.detail = message
                        _emit_port(emit, result)
                        _log(
                            emit,
                            "error",
                            f"{result.config.port} {message}",
                            index=result.index,
                        )
                    if unresolved and failure["reason"] is None:
                        first = unresolved[0]
                        set_failure(
                            f"{first.config.port} {first.error or '未收到有效响应'}",
                            cycle_number,
                        )

                    # 本轮所有通道的结果都已收集，记录失败后停止
                    break
            finally:
                read_stop.set()
                for reader in readers:
                    reader.join(timeout=READ_TIMEOUT + 1.5)

            # 5. 汇总失败原因与未完成本轮的状态
            if report.aborted_reason is None and failure["reason"]:
                report.failure_reason = failure["reason"]
                report.failed_round = failure["round"] or cycle_number
            for result in results:
                if result.status in (STATUS_PASSED, STATUS_FAILED, STATUS_STOPPED):
                    continue
                if report.aborted_reason or report.failure_reason:
                    result.status = STATUS_STOPPED
                    result.detail = "测试中止，未完成本轮判定"
                elif stop_event.is_set():
                    result.status = STATUS_STOPPED
                    if report.cycles_completed:
                        result.detail = (
                            f"手动停止，本轮未完成"
                            f"（此前已完成 {report.cycles_completed} 轮）"
                        )
                    else:
                        result.detail = "手动停止，本轮未完成"
                else:
                    result.status = STATUS_STOPPED
                    result.detail = "测试结束，未完成本轮判定"
                _emit_port(emit, result)

            if report.aborted_reason or report.failure_reason:
                return finish("error")
            if report.stopped:
                return finish("success" if report.passed else "warning")
            return finish("success" if report.passed else "warning")
        finally:
            read_stop.set()
            for _result, handle in opened:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001 - 关闭异常不影响测试结论
                    pass

    @staticmethod
    def _flush_inputs(opened: Sequence[tuple[PortResult, Any]]) -> None:
        """清空本轮请求前遗留的接收缓存，避免迟到帧被计入新一轮。"""
        for _result, handle in opened:
            try:
                handle.reset_input_buffer()
            except Exception:  # noqa: BLE001 - 个别驱动不支持清理缓存
                pass

    def _open_all(
        self, results: Sequence[PortResult], emit: EventCallback
    ) -> tuple[list[tuple[PortResult, Any]], dict[int, str]]:
        """三个线程并行打开全部串口，返回成功句柄与各端口失败信息。"""
        handles: dict[int, Any] = {}
        errors: dict[int, str] = {}
        barrier = threading.Barrier(len(results))

        def worker(result: PortResult) -> None:
            config = result.config
            try:
                barrier.wait(timeout=SEND_BARRIER_TIMEOUT)
            except threading.BrokenBarrierError:
                errors[result.index] = "打开线程同步失败"
            else:
                try:
                    handle = self._serial_factory(
                        port=config.port,
                        baudrate=config.baudrate,
                        bytesize=config.bytesize,
                        parity=config.parity,
                        stopbits=config.stopbits,
                        xonxoff=config.xonxoff,
                        rtscts=config.rtscts,
                        dsrdtr=config.dsrdtr,
                        timeout=READ_TIMEOUT,
                        write_timeout=WRITE_TIMEOUT,
                    )
                    handle.reset_input_buffer()
                except Exception as exc:  # noqa: BLE001 - 统一转为失败信息
                    errors[result.index] = f"打开串口失败：{exc}"
                else:
                    handles[result.index] = handle
                    result.status = STATUS_IDLE
                    result.detail = "串口已打开"
                    _emit_port(emit, result)
                    _log(emit, "info", f"{config.port} 打开成功", index=result.index)
                    return

            result.status = STATUS_FAILED
            result.error = errors[result.index]
            result.detail = result.error
            _emit_port(emit, result)
            _log(
                emit,
                "error",
                f"{config.port} {result.error}",
                index=result.index,
            )

        threads = [
            threading.Thread(target=worker, args=(result,), daemon=True)
            for result in results
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=SEND_BARRIER_TIMEOUT + 2.0)

        # 极端情况下线程未在超时内返回，按打开失败处理，避免测试悬挂
        for result in results:
            if result.index in handles or result.index in errors:
                continue
            errors[result.index] = "打开串口超时"
            result.status = STATUS_FAILED
            result.error = errors[result.index]
            result.detail = result.error
            _emit_port(emit, result)
            _log(
                emit,
                "error",
                f"{result.config.port} {result.error}",
                index=result.index,
            )

        opened = [
            (result, handles[result.index])
            for result in results
            if result.index in handles
        ]
        return opened, errors

    def _send_all(
        self,
        opened: Sequence[tuple[PortResult, Any]],
        request_frame: bytes,
    ) -> dict[int, str]:
        """用屏障让所有串口在同一时刻写出指令，返回各端口的发送错误。"""
        errors: dict[int, str] = {}
        barrier = threading.Barrier(len(opened) + 1)

        def worker(result: PortResult, handle: Any) -> None:
            try:
                barrier.wait(timeout=SEND_BARRIER_TIMEOUT)
            except threading.BrokenBarrierError:
                errors[result.index] = "发送线程同步失败"
                return
            try:
                handle.write(request_frame)
                handle.flush()
            except Exception as exc:  # noqa: BLE001
                errors[result.index] = f"发送失败：{exc}"

        threads = [
            threading.Thread(target=worker, args=(result, handle), daemon=True)
            for result, handle in opened
        ]
        for thread in threads:
            thread.start()
        try:
            barrier.wait(timeout=SEND_BARRIER_TIMEOUT)
        except threading.BrokenBarrierError:
            pass
        for thread in threads:
            thread.join(timeout=WRITE_TIMEOUT + 2.0)
        return errors

    def _listen_port(
        self,
        result: PortResult,
        handle: Any,
        read_stop: threading.Event,
        stop_event: threading.Event,
        emit: EventCallback,
        lock: threading.Lock,
        set_failure: Callable[[str, int | None], None],
        response_template: Sequence[int | None],
    ) -> None:
        """常驻监听线程：按期望响应模板解析帧，按当前轮次记录结果。"""
        parser = FrameParser(response_template)
        while not read_stop.is_set() and not stop_event.is_set():
            try:
                pending = handle.in_waiting
            except Exception:  # noqa: BLE001 - 个别驱动不支持查询缓存
                pending = 0
            try:
                data = handle.read(pending if pending else 1)
            except Exception as exc:  # noqa: BLE001
                message = f"读取串口失败：{exc}"
                with lock:
                    result.status = STATUS_FAILED
                    result.error = message
                    result.detail = message
                    result.cycle_active = False
                    round_no = result.cycle_number
                set_failure(f"{result.config.port} {message}", round_no)
                _emit_port(emit, result)
                _log(
                    emit,
                    "error",
                    f"{result.config.port} {message}",
                    index=result.index,
                )
                return
            if not data:
                continue
            with lock:
                if result.cycle_active:
                    result.cycle_rx_count += len(data)
                    remaining = RX_PREVIEW_LIMIT - len(result.cycle_rx_preview)
                    if remaining > 0:
                        result.cycle_rx_preview += data[:remaining]
            for frame in parser.feed(data):
                if not self._handle_frame(
                    result, frame, emit, lock, set_failure, response_template
                ):
                    return

    def _handle_frame(
        self,
        result: PortResult,
        frame: bytes,
        emit: EventCallback,
        lock: threading.Lock,
        set_failure: Callable[[str, int | None], None],
        response_template: Sequence[int | None],
    ) -> bool:
        """校验一帧响应；返回 False 表示该端口已判定失败。"""
        config = result.config
        expected_frame = render_response(response_template, config.expected_com)
        wrong = frame != expected_frame

        with lock:
            round_no = result.cycle_number
            active = result.cycle_active
            already = result.cycle_responded
            count = result.response_count
            message = ""
            if active:
                if wrong:
                    message = (
                        f"第 {round_no} 轮响应错误：期望 "
                        f"{format_hex(expected_frame)}，"
                        f"实际 {format_hex(frame)}"
                    )
                    result.status = STATUS_FAILED
                    result.error = message
                    result.detail = message
                    result.cycle_active = False
                else:
                    result.response = frame
                    result.response_count += 1
                    count = result.response_count
                    result.cycle_responded = True
                    result.status = STATUS_PASSED
                    result.detail = f"第 {round_no} 轮响应正确"

        if not active:
            # 非本轮请求窗口内到达的帧不参与判定，避免迟到的响应串入下一轮
            level = "warning" if wrong else "info"
            hint = "异常帧" if wrong else "额外帧"
            _log(
                emit,
                level,
                f"轮间收到{hint}（已忽略）：{format_hex(frame)}",
                index=result.index,
            )
            return True

        if wrong:
            set_failure(f"{config.port} {message}", round_no)
            _emit_port(emit, result)
            _log(emit, "error", f"{config.port} {message}", index=result.index)
            return False

        if already:
            _log(
                emit,
                "info",
                f"第 {round_no} 轮重复收到正确响应（累计 {count} 帧）：{format_hex(frame)}",
                index=result.index,
            )
        else:
            _log(
                emit,
                "success",
                f"第 {round_no} 轮收到正确响应：{format_hex(frame)}",
                index=result.index,
            )
        _emit_port(emit, result)
        return True
