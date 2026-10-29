"""多通道轮询测试流程的单元测试（使用模拟串口，无需真实硬件）。"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Callable

import serial

from app.protocol import REQUEST_FRAME, build_response_frame
from app.tester import (
    STATUS_FAILED,
    STATUS_NOT_TESTED,
    STATUS_PASSED,
    STATUS_STOPPED,
    PortConfig,
    SerialTester,
    describe_serial,
)


class FakeSerial:
    """模拟 pyserial.Serial 的最小实现，可在收到指令后按预约时间回帧。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        timeout: float = 0.05,
        write_timeout: float = 1.0,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.xonxoff = xonxoff
        self.rtscts = rtscts
        self.dsrdtr = dsrdtr
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.written: list[bytes] = []
        self.write_times: list[float] = []
        self.closed = False
        self.on_write: Callable[[bytes], list[tuple[float, bytes]]] | None = None
        self._rx = bytearray()
        self._lock = threading.Lock()

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        # 与真实串口一致：无数据时最多阻塞 timeout 秒
        deadline = time.monotonic() + (self.timeout or 0.05)
        while True:
            with self._lock:
                if self._rx:
                    count = min(max(size, 1), len(self._rx))
                    data = bytes(self._rx[:count])
                    del self._rx[:count]
                    return data
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.002)

    def write(self, data: bytes) -> int:
        payload = bytes(data)
        self.written.append(payload)
        self.write_times.append(time.monotonic())
        if self.on_write is not None:
            for delay, response in self.on_write(payload):
                timer = threading.Timer(delay, self.inject, args=(response,))
                timer.daemon = True
                timer.start()
        return len(payload)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def inject(self, payload: bytes) -> None:
        with self._lock:
            self._rx.extend(payload)


def make_factory(
    behaviors: dict[str, Callable[[bytes], list[tuple[float, bytes]]]] | None = None,
    fail_ports: tuple[str, ...] = (),
):
    """返回 (串口工厂, 已创建模拟串口字典)。"""
    created: dict[str, FakeSerial] = {}
    behaviors = behaviors or {}

    def factory(**kwargs):
        port = kwargs["port"]
        if port in fail_ports:
            raise serial.SerialException("拒绝访问（模拟）")
        device = FakeSerial(**kwargs)
        behavior = behaviors.get(port)
        if behavior is not None:
            device.on_write = behavior
        created[port] = device
        return device

    return factory, created


def respond(com_no: int, delay: float = 0.02, extra: tuple = ()):
    """生成“收到指令后延迟回复指定 COM 号”的行为。"""

    def behavior(_request: bytes) -> list[tuple[float, bytes]]:
        frames = [(delay, build_response_frame(com_no))]
        frames.extend(extra)
        return frames

    return behavior


def configs() -> list[PortConfig]:
    return [
        PortConfig("COM1", 115200, 1),
        PortConfig("COM2", 115200, 2),
        PortConfig("COM3", 115200, 3),
    ]


class SerialTesterTest(unittest.TestCase):
    def test_requests_repeat_at_interval(self) -> None:
        factory, created = make_factory(
            {"COM1": respond(1), "COM2": respond(2), "COM3": respond(3)}
        )
        events: list[dict] = []
        started = time.monotonic()
        report = SerialTester(serial_factory=factory).run(
            configs(),
            request_interval=0.15,
            response_timeout=0.1,
            max_cycles=3,
            on_event=events.append,
        )

        self.assertTrue(report.passed, report.summary_text())
        self.assertEqual(report.cycles_completed, 3)
        self.assertEqual(report.rounds_attempted, 3)
        self.assertFalse(report.stopped)
        self.assertEqual([result.status for result in report.results], [STATUS_PASSED] * 3)
        for com_no, result in zip((1, 2, 3), report.results):
            self.assertEqual(result.response, build_response_frame(com_no))
            self.assertEqual(result.response_count, 3)

        # 三路每轮都收到同一条指令，且请求按间隔重复触发
        for device in created.values():
            self.assertEqual(device.written, [REQUEST_FRAME] * 3)
            self.assertTrue(device.closed)
            self.assertLess(device.write_times[0] - started, 0.2)  # 首次请求立即触发
            self.assertGreaterEqual(device.write_times[1] - device.write_times[0], 0.1)

        self.assertTrue(any(event["type"] == "report" for event in events))
        self.assertTrue(any(event["type"] == "cycle" for event in events))
        # 端口相关日志必须携带通道号，便于界面按列分发
        port_logs = [
            event for event in events if event["type"] == "log" and "index" in event
        ]
        self.assertTrue(port_logs)
        self.assertEqual({event["index"] for event in port_logs}, {0, 1, 2})
        self.assertGreaterEqual(report.duration, 0.25)

    def test_wrong_com_number_fails_first_round(self) -> None:
        factory, _created = make_factory(
            {
                "COM1": respond(1),
                "COM2": respond(9),  # COM2 回复了错误的 COM 号
                "COM3": respond(3),
            }
        )
        report = SerialTester(serial_factory=factory).run(
            configs(), request_interval=0.2, response_timeout=0.2
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.failed_round, 1)
        self.assertEqual(report.cycles_completed, 0)
        self.assertEqual(report.results[1].status, STATUS_FAILED)
        self.assertIn("响应错误", report.results[1].error or "")
        self.assertIn("09", report.results[1].error or "")
        self.assertIn("COM2", report.failure_reason or "")
        # 失败后仍等本轮其他通道返回，保留完整排查信息
        self.assertEqual(report.results[0].status, STATUS_PASSED)
        self.assertEqual(report.results[2].status, STATUS_PASSED)

    def test_missing_response_fails(self) -> None:
        factory, _created = make_factory({"COM1": respond(1), "COM2": respond(2)})
        started = time.monotonic()
        report = SerialTester(serial_factory=factory).run(
            configs(), request_interval=0.2, response_timeout=0.2
        )
        elapsed = time.monotonic() - started

        self.assertFalse(report.passed)
        self.assertEqual(report.failed_round, 1)
        self.assertEqual(report.results[2].status, STATUS_FAILED)
        self.assertIn("丢失", report.results[2].error or "")
        self.assertIn("丢失", report.results[2].detail)
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertEqual(report.results[0].status, STATUS_PASSED)
        self.assertEqual(report.results[1].status, STATUS_PASSED)

    def test_unmatched_response_reports_raw_data(self) -> None:
        def noise(_request: bytes) -> list[tuple[float, bytes]]:
            return [(0.02, b"\x11\x22\x33")]

        factory, _created = make_factory(
            {"COM1": noise, "COM2": respond(2), "COM3": respond(3)}
        )
        report = SerialTester(serial_factory=factory).run(
            configs(), request_interval=0.1, response_timeout=0.1
        )

        self.assertFalse(report.passed)
        result = report.results[0]
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("收到 3 字节", result.error or "")
        self.assertIn("11 22 33", result.error or "")
        self.assertIn("未匹配期望响应", result.error or "")
        self.assertNotIn("响应丢失", result.error or "")
        # 其他通道本轮结果仍被完整记录
        self.assertEqual(report.results[1].status, STATUS_PASSED)
        self.assertEqual(report.results[2].status, STATUS_PASSED)

    def test_open_failure_aborts_test(self) -> None:
        factory, created = make_factory(
            {"COM1": respond(1)}, fail_ports=("COM2",)
        )
        events: list[dict] = []
        report = SerialTester(serial_factory=factory).run(
            configs(),
            request_interval=0.1,
            response_timeout=0.1,
            on_event=events.append,
        )

        self.assertFalse(report.passed)
        self.assertIn("COM2", report.aborted_reason or "")
        self.assertIn("打开串口失败", report.aborted_reason or "")
        # 三个串口是并行打开的，因此 COM1、COM3 也会被打开后立即关闭
        self.assertEqual(set(created), {"COM1", "COM3"})
        for device in created.values():
            self.assertTrue(device.closed)
            self.assertEqual(device.written, [])  # 打开失败即中止，不应下发指令
        self.assertLess(report.duration, 0.5)

        # 其他通道虽然打开成功，但未开始测试，状态不能停留在“待测试”
        self.assertEqual(report.results[0].status, STATUS_NOT_TESTED)
        self.assertEqual(report.results[2].status, STATUS_NOT_TESTED)
        self.assertEqual(report.results[0].detail, "测试中止，未开始测试")
        self.assertNotIn("COM2", report.results[0].detail)
        self.assertNotIn("COM2", report.results[2].detail)

        # COM2 的失败日志只允许出现在通道 2，不能串到其他通道或广播日志
        other_logs = [
            event["message"]
            for event in events
            if event["type"] == "log"
            and (event.get("index") in (0, 2) or "index" not in event)
        ]
        self.assertFalse(
            any("COM2" in message for message in other_logs), other_logs
        )
        com2_logs = [
            event["message"]
            for event in events
            if event["type"] == "log" and event.get("index") == 1
        ]
        self.assertTrue(any("打开串口失败" in message for message in com2_logs))

    def test_ports_are_opened_in_parallel(self) -> None:
        base_factory, _created = make_factory(
            {"COM1": respond(1), "COM2": respond(2), "COM3": respond(3)}
        )
        thread_ids: list[int] = []
        lock = threading.Lock()

        def recording_factory(**kwargs):
            with lock:
                thread_ids.append(threading.get_ident())
            time.sleep(0.02)  # 拉长打开耗时，暴露串行打开问题
            return base_factory(**kwargs)

        events: list[dict] = []
        report = SerialTester(serial_factory=recording_factory).run(
            configs(),
            request_interval=0.1,
            response_timeout=0.1,
            max_cycles=1,
            on_event=events.append,
        )

        self.assertTrue(report.passed, report.summary_text())
        # 三个串口必须由三个不同线程同时打开
        self.assertEqual(len(set(thread_ids)), 3)

        logs = [event for event in events if event["type"] == "log"]
        open_logs = [i for i, event in enumerate(logs) if "正在打开" in event["message"]]
        success_logs = [i for i, event in enumerate(logs) if "打开成功" in event["message"]]
        send_logs = [i for i, event in enumerate(logs) if "同时发送指令" in event["message"]]
        self.assertEqual(len(open_logs), 3)
        self.assertEqual(len(success_logs), 3)
        # 三条“正在打开”日志都先于任何“打开成功”，说明打开动作是同时发起的
        self.assertLess(max(open_logs), min(success_logs))
        # 三个串口全部打开完成后才下发指令
        self.assertGreater(min(send_logs), max(success_logs))

    def test_describe_serial(self) -> None:
        config = PortConfig(
            "COM1",
            9600,
            1,
            bytesize=7,
            parity="E",
            stopbits=2,
            xonxoff=True,
            rtscts=True,
            dsrdtr=True,
        )
        self.assertEqual(
            describe_serial(config), "9600 7E2 / XON/XOFF+RTS/CTS+DSR/DTR"
        )

    def test_serial_link_parameters_passed_per_port(self) -> None:
        factory, created = make_factory(
            {"COM1": respond(1), "COM2": respond(2), "COM3": respond(3)}
        )
        custom = [
            PortConfig(
                "COM1",
                115200,
                1,
                bytesize=7,
                parity="E",
                stopbits=2,
                xonxoff=True,
                rtscts=True,
                dsrdtr=True,
            ),
            PortConfig("COM2", 115200, 2),
            PortConfig("COM3", 115200, 3),
        ]
        report = SerialTester(serial_factory=factory).run(
            custom, request_interval=0.1, response_timeout=0.1, max_cycles=1
        )

        self.assertTrue(report.passed, report.summary_text())
        com1 = created["COM1"]
        self.assertEqual((com1.bytesize, com1.parity, com1.stopbits), (7, "E", 2))
        self.assertTrue(com1.xonxoff)
        self.assertTrue(com1.rtscts)
        self.assertTrue(com1.dsrdtr)

        com2 = created["COM2"]
        self.assertEqual((com2.bytesize, com2.parity, com2.stopbits), (8, "N", 1))
        self.assertFalse(com2.xonxoff)
        self.assertFalse(com2.rtscts)
        self.assertFalse(com2.dsrdtr)

    def test_custom_request_and_response_pattern(self) -> None:
        def custom_respond(com_no: int):
            def behavior(_request: bytes) -> list[tuple[float, bytes]]:
                return [(0.02, bytes([0xAA, com_no, 0xBB]))]

            return behavior

        factory, created = make_factory(
            {
                "COM1": custom_respond(1),
                "COM2": custom_respond(2),
                "COM3": custom_respond(3),
            }
        )
        request_frame = bytes.fromhex("01 02 03")
        report = SerialTester(serial_factory=factory).run(
            configs(),
            request_interval=0.1,
            response_timeout=0.1,
            request_frame=request_frame,
            response_template=(0xAA, None, 0xBB),
            max_cycles=1,
        )

        self.assertTrue(report.passed, report.summary_text())
        for com_no, result in zip((1, 2, 3), report.results):
            self.assertEqual(result.response, bytes([0xAA, com_no, 0xBB]))
        for device in created.values():
            self.assertEqual(device.written, [request_frame])

    def test_invalid_frame_settings_rejected(self) -> None:
        factory, created = make_factory()
        tester = SerialTester(serial_factory=factory)
        with self.assertRaises(ValueError):
            tester.run(
                configs(),
                request_interval=0.1,
                response_timeout=0.1,
                request_frame=b"",
            )
        with self.assertRaises(ValueError):
            tester.run(
                configs(),
                request_interval=0.1,
                response_timeout=0.1,
                response_template=(0xAA, 0xBB),
            )
        with self.assertRaises(ValueError):
            tester.run(
                configs(),
                request_interval=0.1,
                response_timeout=0.1,
                response_template=(None, None),
            )
        self.assertEqual(created, {})

    def test_wrong_frame_during_active_cycle_fails(self) -> None:
        factory, _created = make_factory(
            {
                # COM1 先回正确帧，再在 COM2/COM3 仍等待时回一帧错误 COM 号
                "COM1": respond(1, extra=((0.06, build_response_frame(0x09)),)),
                "COM2": respond(2, delay=0.1),
                "COM3": respond(3, delay=0.1),
            }
        )
        started = time.monotonic()
        report = SerialTester(serial_factory=factory).run(
            configs(),
            request_interval=0.2,
            response_timeout=0.15,
            max_cycles=5,  # 防止逻辑异常时用例无限运行
        )
        elapsed = time.monotonic() - started

        self.assertFalse(report.passed)
        self.assertEqual(report.results[0].status, STATUS_FAILED)
        self.assertIn("响应错误", report.results[0].error or "")
        # 失败后仍等待 COM2/COM3 返回，保证本轮诊断信息完整
        self.assertEqual(report.results[1].status, STATUS_PASSED)
        self.assertEqual(report.results[2].status, STATUS_PASSED)
        self.assertGreaterEqual(elapsed, 0.08)

    def test_invalid_timing_settings_rejected(self) -> None:
        factory, created = make_factory()
        tester = SerialTester(serial_factory=factory)
        with self.assertRaises(ValueError):
            tester.run(configs(), request_interval=0.1, response_timeout=0.2)
        with self.assertRaises(ValueError):
            tester.run(configs(), request_interval=0.0, response_timeout=0.1)
        with self.assertRaises(ValueError):
            tester.run(configs(), request_interval=0.2, response_timeout=0.0)
        self.assertEqual(created, {})

    def test_late_frames_outside_cycle_are_ignored(self) -> None:
        factory, _created = make_factory(
            {
                # 每轮除正常响应外，额外伪造一帧迟到的正确响应
                "COM1": respond(1, extra=((0.2, build_response_frame(1)),)),
                "COM2": respond(2),
                "COM3": respond(3),
            }
        )
        events: list[dict] = []
        report = SerialTester(serial_factory=factory).run(
            configs(),
            request_interval=0.4,
            response_timeout=0.1,
            max_cycles=2,
            on_event=events.append,
        )

        self.assertTrue(report.passed, report.summary_text())
        self.assertEqual(report.cycles_completed, 2)
        # 每轮只应计 1 帧，迟到的额外帧被忽略，不会串入下一轮
        self.assertEqual(report.results[0].response_count, 2)
        ignored = [
            event
            for event in events
            if event["type"] == "log" and "已忽略" in event["message"]
        ]
        self.assertTrue(ignored)

    def test_manual_stop_marks_unfinished_ports_stopped(self) -> None:
        factory, created = make_factory()
        stop_event = threading.Event()
        stopper = threading.Timer(0.12, stop_event.set)
        stopper.daemon = True
        stopper.start()

        started = time.monotonic()
        report = SerialTester(serial_factory=factory).run(
            configs(),
            request_interval=5.0,
            response_timeout=5.0,
            stop_event=stop_event,
        )
        elapsed = time.monotonic() - started

        self.assertTrue(report.stopped)
        self.assertFalse(report.passed)
        self.assertEqual(report.cycles_completed, 0)
        self.assertEqual([result.status for result in report.results], [STATUS_STOPPED] * 3)
        self.assertLess(elapsed, 2.0)
        for device in created.values():
            self.assertTrue(device.closed)

    def test_manual_stop_after_successful_rounds_passes(self) -> None:
        factory, _created = make_factory(
            {"COM1": respond(1), "COM2": respond(2), "COM3": respond(3)}
        )
        stop_event = threading.Event()
        stopper = threading.Timer(0.35, stop_event.set)
        stopper.daemon = True
        stopper.start()

        report = SerialTester(serial_factory=factory).run(
            configs(),
            request_interval=0.1,
            response_timeout=0.1,
            stop_event=stop_event,
        )

        self.assertTrue(report.stopped)
        self.assertTrue(report.passed, report.summary_text())
        self.assertGreaterEqual(report.cycles_completed, 2)

    def test_duplicate_port_names_are_rejected(self) -> None:
        factory, created = make_factory()
        duplicate = [
            PortConfig("COM1", 115200, 1),
            PortConfig("COM1", 115200, 1),
            PortConfig("COM2", 115200, 2),
        ]
        report = SerialTester(serial_factory=factory).run(
            duplicate, request_interval=0.1, response_timeout=0.1
        )

        self.assertFalse(report.passed)
        self.assertIn("重复", report.aborted_reason or "")
        self.assertEqual(created, {})
        self.assertLess(report.duration, 0.2)
        # 配置校验失败导致中止时，所有通道都应显示“未测试”
        self.assertEqual(
            [result.status for result in report.results], [STATUS_NOT_TESTED] * 3
        )


if __name__ == "__main__":
    unittest.main()
