"""界面构建与三列卡片渲染的冒烟测试（无图形环境时自动跳过）。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config_store import AppSettings, ChannelSettings, load_settings, save_settings
from app.protocol import DEFAULT_RESPONSE_TEMPLATE, build_response_frame
from app.tester import (
    STATUS_FAILED,
    STATUS_IDLE,
    STATUS_NOT_TESTED,
    STATUS_PASSED,
    PortConfig,
    PortResult,
    TestReport,
)


class GuiSmokeTest(unittest.TestCase):
    def _create_app(self, config_path: Path | None = None):
        try:
            import tkinter as tk
        except ImportError as exc:  # pragma: no cover - 依赖环境
            self.skipTest(f"tkinter 不可用：{exc}")
            return None, None
        try:
            root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 依赖环境
            self.skipTest(f"无图形界面环境：{exc}")
            return None, None
        root.withdraw()
        from app.gui import SerialTestApp

        target = config_path
        if target is None:
            target = Path(__file__).resolve().parent / "_no_config.json"
            target.unlink(missing_ok=True)
        with patch("app.gui.SerialTestApp._config_path", return_value=target):
            app = SerialTestApp(root)
        return app, root

    def test_app_builds_three_columns_with_cards(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            self.assertEqual(len(app.rows), 3)
            self.assertEqual(app._port_device(app.rows[0].port_var.get()), "COM1")
            self.assertEqual(app.rows[0].baud_var.get(), "115200")
            # 每列都有独立的状态卡片与日志卡片
            logs = {id(row.log_text) for row in app.rows}
            statuses = {id(row.status_label) for row in app.rows}
            self.assertEqual(len(logs), 3)
            self.assertEqual(len(statuses), 3)
            for row in app.rows:
                self.assertEqual(row.status_label.cget("text"), STATUS_IDLE)
        finally:
            root.destroy()

    def test_status_card_rendering(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            app._render_row(
                app.rows[0], STATUS_PASSED, "响应正确（COM号 0x01）", 1, build_response_frame(1)
            )
            self.assertEqual(app.rows[0].status_label.cget("text"), STATUS_PASSED)
            self.assertIn("FA FB FC FD 00 01 FE FF", app.rows[0].response_label.cget("text"))
            self.assertIn("帧数 1", app.rows[0].count_label.cget("text"))

            app._render_row(app.rows[1], STATUS_FAILED, "响应错误", 0, None)
            self.assertEqual(app.rows[1].status_label.cget("text"), STATUS_FAILED)
            self.assertIn("响应帧：—", app.rows[1].response_label.cget("text"))

            app._render_row(
                app.rows[2], STATUS_NOT_TESTED, "测试中止，未开始测试", 0, None
            )
            self.assertEqual(app.rows[2].status_label.cget("text"), STATUS_NOT_TESTED)
            self.assertIn("测试中止，未开始测试", app.rows[2].detail_label.cget("text"))
        finally:
            root.destroy()

    def test_serial_parameter_widgets_have_defaults(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            for row in app.rows:
                self.assertEqual(row.baud_var.get(), "115200")
                self.assertEqual(row.data_bits_var.get(), "8")
                self.assertEqual(row.parity_var.get(), "无 (N)")
                self.assertEqual(row.stop_bits_var.get(), "1")
                self.assertFalse(row.xonxoff_var.get())
                self.assertFalse(row.rtscts_var.get())
                self.assertFalse(row.dsrdtr_var.get())
        finally:
            root.destroy()

    def test_sync_serial_settings_to_other_channels(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            source = app.rows[1]
            source.baud_var.set("9600")
            source.data_bits_var.set("7")
            source.parity_var.set("偶 (E)")
            source.stop_bits_var.set("2")
            source.xonxoff_var.set(True)
            source.rtscts_var.set(True)
            source.dsrdtr_var.set(False)

            app._sync_channel_settings(source)

            for row in app.rows:
                if row is source:
                    continue
                self.assertEqual(row.baud_var.get(), "9600")
                self.assertEqual(row.data_bits_var.get(), "7")
                self.assertEqual(row.parity_var.get(), "偶 (E)")
                self.assertEqual(row.stop_bits_var.get(), "2")
                self.assertTrue(row.xonxoff_var.get())
                self.assertTrue(row.rtscts_var.get())
                self.assertFalse(row.dsrdtr_var.get())

            # 同步不应改变各通道的串口名与期望 COM 号
            self.assertEqual(
                app._port_device(app.rows[0].port_var.get()), "COM1"
            )
            self.assertEqual(
                app._port_device(app.rows[2].port_var.get()), "COM3"
            )
            self.assertEqual(app.rows[0].com_var.get(), 1)
            self.assertEqual(app.rows[2].com_var.get(), 3)

            # 同步提示会广播到三列日志
            self.assertIn("同步至其他", app.rows[0].log_text.get("1.0", "end"))
            self.assertIn("同步至其他", app.rows[2].log_text.get("1.0", "end"))
        finally:
            root.destroy()

    def test_frame_config_widgets_and_parsing(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            self.assertEqual(app.command_var.get(), "FA FB FC FD 00 00 FE FF")
            self.assertEqual(
                app.response_pattern_var.get(), "FA FB FC FD 00 {COM} FE FF"
            )
            request_frame, template = app._parse_frame_settings()
            self.assertEqual(request_frame, bytes.fromhex("FA FB FC FD 00 00 FE FF"))
            self.assertEqual(template, DEFAULT_RESPONSE_TEMPLATE)

            app.command_var.set("01 02 03")
            app.response_pattern_var.set("AA 55 {com} FE FF")
            request_frame, template = app._parse_frame_settings()
            self.assertEqual(request_frame, b"\x01\x02\x03")
            self.assertEqual(template, (0xAA, 0x55, None, 0xFE, 0xFF))

            app.response_pattern_var.set("AA 55 FE FF")
            with self.assertRaises(ValueError):
                app._parse_frame_settings()
            app.command_var.set("GG")
            with self.assertRaises(ValueError):
                app._parse_frame_settings()
        finally:
            root.destroy()

    def test_default_global_timing_settings(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            self.assertEqual(app.interval_var.get(), "2.0")
            self.assertEqual(app.timeout_var.get(), "2.0")
            self.assertEqual(app.target_rounds_var.get(), "0")
        finally:
            root.destroy()

    def test_cycle_event_updates_verdict(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            app._handle_event({"type": "cycle", "round": 5})
            self.assertEqual(app.verdict_label.cget("text"), "测试中")
            self.assertIn("第 5 轮", app.verdict_detail_label.cget("text"))
        finally:
            root.destroy()

    def test_global_verdict_four_states(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            self.assertEqual(app.verdict_label.cget("text"), "待启动测试")
            self.assertIn("尚未开始", app.verdict_detail_label.cget("text"))

            passed_report = TestReport(
                results=[
                    PortResult(
                        index=0,
                        config=PortConfig("COM1", expected_com=1),
                        status=STATUS_PASSED,
                    )
                ],
                cycles_completed=3,
            )
            app._handle_report(passed_report)
            self.assertEqual(app.verdict_label.cget("text"), "合格")
            self.assertIn("1 通道 · 3 轮", app.verdict_detail_label.cget("text"))

            # 单路失败事件到达时立即判不合格，不必等本轮汇总
            app._handle_event(
                {
                    "type": "port",
                    "index": 0,
                    "status": STATUS_FAILED,
                    "detail": "响应错误",
                    "count": 0,
                    "response": None,
                    "round": 2,
                }
            )
            self.assertEqual(app.verdict_label.cget("text"), "不合格")
            self.assertIn("第 2 轮", app.verdict_detail_label.cget("text"))

            failed_report = TestReport(
                results=[
                    PortResult(
                        index=0,
                        config=PortConfig("COM1", expected_com=1),
                        status=STATUS_FAILED,
                    )
                ],
                failure_reason="COM1 响应错误",
                failed_round=2,
                cycles_completed=1,
            )
            app._handle_report(failed_report)
            self.assertEqual(app.verdict_label.cget("text"), "不合格")
            self.assertIn("第 2 轮", app.verdict_detail_label.cget("text"))
            self.assertIn("COM1", app.verdict_detail_label.cget("text"))

            aborted_report = TestReport(
                results=[
                    PortResult(
                        index=0,
                        config=PortConfig("COM2", expected_com=2),
                        status=STATUS_NOT_TESTED,
                    )
                ],
                aborted_reason="COM2 打开串口失败",
            )
            app._handle_report(aborted_report)
            self.assertEqual(app.verdict_label.cget("text"), "不合格")

            stopped_report = TestReport(
                results=[
                    PortResult(
                        index=0,
                        config=PortConfig("COM1", expected_com=1),
                        status=STATUS_IDLE,
                    )
                ],
                stopped=True,
                cycles_completed=0,
            )
            app._handle_report(stopped_report)
            self.assertEqual(app.verdict_label.cget("text"), "待启动测试")
        finally:
            root.destroy()

    def test_add_and_remove_channels(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            app.add_channel()
            self.assertEqual(len(app.rows), 4)
            self.assertEqual(app.rows[-1].title_label.cget("text"), "通道 4")

            app.remove_channel(app.rows[1])
            self.assertEqual(len(app.rows), 3)
            self.assertEqual(
                [row.title_label.cget("text") for row in app.rows],
                ["通道 1", "通道 2", "通道 3"],
            )
            self.assertEqual([row.index for row in app.rows], [0, 1, 2])

            app.remove_channel(app.rows[-1])
            app.remove_channel(app.rows[-1])
            self.assertEqual(len(app.rows), 1)
            self.assertEqual(app.rows[0].delete_btn.cget("state"), "disabled")

            # 至少保留一个通道
            app.remove_channel(app.rows[0])
            self.assertEqual(len(app.rows), 1)
        finally:
            root.destroy()

    def test_export_logs_splits_by_channel(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        tmpdir = Path(
            tempfile.mkdtemp(
                prefix="log_export_", dir=str(Path(__file__).resolve().parent)
            )
        )
        try:
            app.add_channel()  # 4 个通道，验证按通道数拆分
            app._clear_logs()
            app._append_log("广播日志", "info")
            app._append_log("通道2专属日志", "error", index=1)

            with patch(
                "app.gui.SerialTestApp._export_log_dir", return_value=tmpdir
            ), patch("app.gui.messagebox.showinfo") as info_mock:
                app.export_logs()

            files = sorted(tmpdir.glob("*.log"))
            self.assertEqual(len(files), 4)
            self.assertEqual(info_mock.call_count, 1)
            names = [path.name for path in files]
            for index in range(1, 5):
                self.assertTrue(
                    any(name.startswith(f"通道{index}_") for name in names), names
                )

            first = next(path for path in files if path.name.startswith("通道1_"))
            second = next(path for path in files if path.name.startswith("通道2_"))
            first_text = first.read_text(encoding="utf-8-sig")
            second_text = second.read_text(encoding="utf-8-sig")

            self.assertIn("广播日志", first_text)
            self.assertIn("通道2专属日志", second_text)
            self.assertNotIn("通道2专属日志", first_text)
            self.assertIn("串口 COM1", first_text)
            self.assertIn("波特率 115200", first_text)
            self.assertIn("导出时间", first_text)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            root.destroy()

    def test_export_logs_reports_when_program_dir_not_writable(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        tmpdir = Path(
            tempfile.mkdtemp(
                prefix="log_write_fail_", dir=str(Path(__file__).resolve().parent)
            )
        )
        blocker = tmpdir / "not_a_dir"
        blocker.write_text("blocked", encoding="utf-8")
        try:
            with patch(
                "app.gui.SerialTestApp._export_log_dir", return_value=blocker
            ), patch("app.gui.messagebox.showerror") as error_mock, patch(
                "app.gui.messagebox.showinfo"
            ) as info_mock:
                app.export_logs()

            self.assertEqual(error_mock.call_count, 1)
            self.assertEqual(info_mock.call_count, 0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            root.destroy()

    def test_port_dropdown_shows_device_description(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        fake_ports = [
            SimpleNamespace(
                device="COM1",
                description="Prolific USB-to-Serial Comm Port (COM1)",
            ),
            SimpleNamespace(
                device="COM2", description="USB-SERIAL CH340 (COM2)"
            ),
            SimpleNamespace(
                device="COM3", description="USB-SERIAL CH344 (COM3)"
            ),
        ]
        try:
            with patch("app.gui.list_ports.comports", return_value=fake_ports):
                app.refresh_ports(silent=True)

            expected = "COM1 - Prolific"
            self.assertEqual(app.rows[0].port_var.get(), expected)
            self.assertIn(expected, app.rows[0].combo.cget("values"))
            self.assertIn(
                "COM2 - CH340", app.rows[1].combo.cget("values")
            )
            # 展示带名称，但保存配置时必须还原为真实串口名
            settings = app._collect_settings()
            self.assertEqual(
                [channel.port for channel in settings.channels],
                ["COM1", "COM2", "COM3"],
            )
        finally:
            root.destroy()

    def test_add_channel_refreshes_ports_automatically(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            fake_ports = [
                SimpleNamespace(device="COM1"),
                SimpleNamespace(device="COM9"),
            ]
            with patch("app.gui.list_ports.comports", return_value=fake_ports):
                app.add_channel()
            self.assertEqual(len(app.rows), 4)
            self.assertEqual(app.rows[-1].port_var.get(), "COM9")
            # 新增列自身也必须带上下拉可选项，而不只是旧列被刷新
            self.assertIn("COM9", app.rows[-1].combo.cget("values"))
            self.assertIn("COM9", app.rows[0].combo.cget("values"))
        finally:
            root.destroy()

    def test_startup_loads_config_file(self) -> None:
        tmpdir = Path(
            tempfile.mkdtemp(
                prefix="cfg_startup_", dir=str(Path(__file__).resolve().parent)
            )
        )
        config_path = tmpdir / "config.json"
        settings = AppSettings(
            request_interval=0.8,
            response_timeout=0.4,
            command="01 02",
            response_pattern="AA {COM} BB",
            target_rounds=5,
            channels=[
                ChannelSettings(
                    "COM5", 5, 9600, 7, "E", 2.0, xonxoff=True, rtscts=True
                ),
                ChannelSettings("COM6", 6, 115200, 8, "N", 1.0),
            ],
        )
        save_settings(config_path, settings)
        app, root = self._create_app(config_path=config_path)
        if app is None:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return
        try:
            self.assertEqual(len(app.rows), 2)
            self.assertEqual(
                app._port_device(app.rows[0].port_var.get()), "COM5"
            )
            self.assertEqual(app.rows[0].com_var.get(), 5)
            self.assertEqual(app.rows[0].baud_var.get(), "9600")
            self.assertEqual(app.rows[0].data_bits_var.get(), "7")
            self.assertEqual(app.rows[0].parity_var.get(), "偶 (E)")
            self.assertEqual(app.rows[0].stop_bits_var.get(), "2")
            self.assertTrue(app.rows[0].xonxoff_var.get())
            self.assertTrue(app.rows[0].rtscts_var.get())
            self.assertFalse(app.rows[0].dsrdtr_var.get())
            self.assertEqual(app.interval_var.get(), "0.8")
            self.assertEqual(app.timeout_var.get(), "0.4")
            self.assertEqual(app.target_rounds_var.get(), "5")
            self.assertEqual(app.command_var.get(), "01 02")
            self.assertEqual(app.response_pattern_var.get(), "AA {COM} BB")
        finally:
            root.destroy()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_save_current_config_writes_file(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        tmpdir = Path(
            tempfile.mkdtemp(
                prefix="cfg_save_", dir=str(Path(__file__).resolve().parent)
            )
        )
        target = tmpdir / "config.json"
        try:
            with patch("app.gui.SerialTestApp._config_path", return_value=target):
                path = app._save_current_config()
            self.assertEqual(path, target)
            loaded = load_settings(target)
            self.assertEqual(len(loaded.channels), 3)
            self.assertEqual(loaded.command, "FA FB FC FD 00 00 FE FF")
            self.assertEqual(
                loaded.response_pattern, "FA FB FC FD 00 {COM} FE FF"
            )
            self.assertEqual(loaded.channels[0].port, "COM1")
            self.assertEqual(loaded.channels[0].expected_com, 1)
            self.assertEqual(loaded.target_rounds, 0)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            root.destroy()

    def test_import_config_applies_and_overwrites(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        tmpdir = Path(
            tempfile.mkdtemp(
                prefix="cfg_import_", dir=str(Path(__file__).resolve().parent)
            )
        )
        source = tmpdir / "source.json"
        target = tmpdir / "config.json"
        settings = AppSettings(
            request_interval=0.8,
            response_timeout=0.4,
            command="01 02",
            response_pattern="AA {COM} BB",
            target_rounds=7,
            channels=[
                ChannelSettings(
                    "COM5", 5, 9600, 7, "E", 2.0, xonxoff=True, rtscts=True
                ),
                ChannelSettings("COM6", 6, 115200, 8, "N", 1.0),
            ],
        )
        save_settings(source, settings)
        try:
            with patch(
                "app.gui.filedialog.askopenfilename", return_value=str(source)
            ), patch(
                "app.gui.SerialTestApp._config_path", return_value=target
            ), patch("app.gui.messagebox.showinfo") as info_mock:
                app.import_config()

            self.assertEqual(len(app.rows), 2)
            self.assertEqual(
                app._port_device(app.rows[0].port_var.get()), "COM5"
            )
            self.assertEqual(app.rows[0].com_var.get(), 5)
            self.assertEqual(app.rows[0].baud_var.get(), "9600")
            self.assertEqual(app.rows[0].parity_var.get(), "偶 (E)")
            self.assertEqual(
                app._port_device(app.rows[1].port_var.get()), "COM6"
            )
            self.assertEqual(app.interval_var.get(), "0.8")
            self.assertEqual(app.target_rounds_var.get(), "7")
            self.assertEqual(app.command_var.get(), "01 02")
            self.assertTrue(target.exists())
            self.assertEqual(load_settings(target), settings)
            self.assertEqual(info_mock.call_count, 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            root.destroy()

    def test_log_routing_per_channel(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            app._clear_logs()
            app._append_log("广播日志", "info")
            app._append_log("通道2专属日志", "error", index=1)

            first = app.rows[0].log_text.get("1.0", "end")
            second = app.rows[1].log_text.get("1.0", "end")
            third = app.rows[2].log_text.get("1.0", "end")

            self.assertIn("广播日志", first)
            self.assertIn("广播日志", second)
            self.assertIn("广播日志", third)
            self.assertNotIn("通道2专属日志", first)
            self.assertIn("通道2专属日志", second)
            self.assertNotIn("通道2专属日志", third)
        finally:
            root.destroy()

    def test_page_vertical_scroll_for_small_screens(self) -> None:
        app, root = self._create_app()
        if app is None:
            return
        try:
            import tkinter as tk

            self.assertIsInstance(app.page_canvas, tk.Canvas)
            self.assertEqual(str(app.page_scroll.cget("orient")), "vertical")
            self.assertTrue(str(app.page_canvas.cget("yscrollcommand")))
            self.assertEqual(app._page_window_id is not None, True)

            root.update_idletasks()
            app._on_page_configure()
            bbox = app.page_canvas.bbox("all")
            self.assertIsNotNone(bbox)
            self.assertGreater(bbox[3], 0)

            # 内容高于可视区时应能向下滚动
            app.page_canvas.configure(scrollregion=(0, 0, 1000, 2000))
            app.page_canvas.yview_moveto(1.0)
            root.update_idletasks()
            self.assertGreater(app.page_canvas.yview()[0], 0.0)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
