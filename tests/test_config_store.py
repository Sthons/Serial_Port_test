"""配置文件读写与校验的单元测试。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.config_store import AppSettings, ChannelSettings, load_settings, save_settings


class ConfigStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(
            tempfile.mkdtemp(
                prefix="cfg_test_", dir=str(Path(__file__).resolve().parent)
            )
        )
        self.path = self.tmpdir / "config.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _settings(self) -> AppSettings:
        return AppSettings(
            request_interval=0.5,
            response_timeout=0.3,
            command="FA FB FC FD 00 00 FE FF",
            response_pattern="AA {COM} BB",
            target_rounds=10,
            channels=[
                ChannelSettings(
                    port="COM3",
                    expected_com=3,
                    baudrate=9600,
                    bytesize=7,
                    parity="E",
                    stopbits=2.0,
                    xonxoff=True,
                    rtscts=True,
                ),
                ChannelSettings(
                    port="COM4",
                    expected_com=4,
                    baudrate=115200,
                    bytesize=8,
                    parity="N",
                    stopbits=1.0,
                ),
            ],
        )

    def test_save_and_load_roundtrip(self) -> None:
        settings = self._settings()
        save_settings(self.path, settings)
        loaded = load_settings(self.path)
        self.assertEqual(loaded, settings)

    def test_invalid_json_rejected(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_settings(self.path)

    def test_missing_channels_rejected(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "request_interval": 1.0,
                    "response_timeout": 0.5,
                    "command": "FA",
                    "response_pattern": "{COM}",
                    "channels": [],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_settings(self.path)

    def test_duplicate_ports_rejected(self) -> None:
        settings = self._settings()
        settings.channels[1].port = "COM3"
        save_settings(self.path, settings)
        with self.assertRaises(ValueError):
            load_settings(self.path)

    def test_invalid_response_pattern_rejected(self) -> None:
        settings = self._settings()
        settings.response_pattern = "AA BB"
        save_settings(self.path, settings)
        with self.assertRaises(ValueError):
            load_settings(self.path)

    def test_timeout_larger_than_interval_rejected(self) -> None:
        settings = self._settings()
        settings.response_timeout = 1.0
        save_settings(self.path, settings)
        with self.assertRaises(ValueError):
            load_settings(self.path)

    def test_missing_target_rounds_defaults_to_unlimited(self) -> None:
        settings = self._settings()
        save_settings(self.path, settings)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw.pop("target_rounds")
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = load_settings(self.path)
        self.assertEqual(loaded.target_rounds, 0)


if __name__ == "__main__":
    unittest.main()
