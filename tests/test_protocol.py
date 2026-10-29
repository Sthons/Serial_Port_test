"""协议组帧与流式解析的单元测试。"""

from __future__ import annotations

import unittest

from app.protocol import (
    COM_PLACEHOLDER,
    DEFAULT_RESPONSE_TEMPLATE,
    FRAME_HEADER,
    REQUEST_FRAME,
    FrameParser,
    build_request_frame,
    build_response_frame,
    format_hex,
    format_response_pattern,
    parse_com_no,
    parse_hex_bytes,
    parse_response_pattern,
    render_response,
)


class ProtocolFrameTest(unittest.TestCase):
    def test_request_frame(self) -> None:
        self.assertEqual(build_request_frame(), bytes.fromhex("FA FB FC FD 00 00 FE FF"))
        self.assertEqual(REQUEST_FRAME, bytes.fromhex("FAFB FCFD 0000 FEFF"))

    def test_build_response_frame(self) -> None:
        self.assertEqual(build_response_frame(1), bytes.fromhex("FA FB FC FD 00 01 FE FF"))
        self.assertEqual(
            build_response_frame(0xFF), bytes.fromhex("FA FB FC FD 00 FF FE FF")
        )

    def test_build_response_frame_rejects_invalid_com(self) -> None:
        with self.assertRaises(ValueError):
            build_response_frame(-1)
        with self.assertRaises(ValueError):
            build_response_frame(256)

    def test_parse_com_no(self) -> None:
        self.assertEqual(parse_com_no(build_response_frame(3)), 3)
        self.assertEqual(parse_com_no(build_response_frame(0xFE)), 0xFE)
        with self.assertRaises(ValueError):
            parse_com_no(b"\x00" * 8)
        with self.assertRaises(ValueError):
            parse_com_no(build_response_frame(3)[:-1])

    def test_format_hex(self) -> None:
        self.assertEqual(format_hex(REQUEST_FRAME), "FA FB FC FD 00 00 FE FF")
        self.assertEqual(format_hex(b""), "")


class FrameParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = FrameParser()

    def test_single_frame(self) -> None:
        frame = build_response_frame(2)
        self.assertEqual(self.parser.feed(frame), [frame])
        self.assertEqual(self.parser.buffer, b"")

    def test_chunked_frame(self) -> None:
        frame = build_response_frame(1)
        frames: list[bytes] = []
        for byte in frame:
            frames.extend(self.parser.feed(bytes([byte])))
        self.assertEqual(frames, [frame])

    def test_noise_before_frame(self) -> None:
        frame = build_response_frame(9)
        self.assertEqual(self.parser.feed(b"\x11\x22\x33" + frame), [frame])

    def test_multiple_frames_in_one_chunk(self) -> None:
        frames = [build_response_frame(1), build_response_frame(2)]
        self.assertEqual(self.parser.feed(b"".join(frames)), frames)

    def test_bad_tail_resync(self) -> None:
        frame = build_response_frame(3)
        noise = FRAME_HEADER + b"\x00\xFE\xFC"
        self.assertEqual(self.parser.feed(noise + frame), [frame])

    def test_partial_header_retained(self) -> None:
        frame = build_response_frame(4)
        self.assertEqual(self.parser.feed(frame[:3]), [])
        self.assertEqual(self.parser.feed(frame[3:]), [frame])

    def test_incomplete_frame_waits(self) -> None:
        frame = build_response_frame(5)
        self.assertEqual(self.parser.feed(frame[:-1]), [])
        self.assertEqual(self.parser.feed(frame[-1:]), [frame])

    def test_reset(self) -> None:
        self.parser.feed(b"\xFA\xFB")
        self.parser.reset()
        self.assertEqual(self.parser.buffer, b"")


class FrameConfigTest(unittest.TestCase):
    def test_parse_hex_bytes(self) -> None:
        self.assertEqual(parse_hex_bytes("FA FB FC FD 00 00 FE FF"), REQUEST_FRAME)
        self.assertEqual(parse_hex_bytes("fafbfcfd0000feff"), REQUEST_FRAME)
        self.assertEqual(parse_hex_bytes("0xFA, 0xFB"), b"\xFA\xFB")
        with self.assertRaises(ValueError):
            parse_hex_bytes("")
        with self.assertRaises(ValueError):
            parse_hex_bytes("FA F")
        with self.assertRaises(ValueError):
            parse_hex_bytes("FA GG")

    def test_parse_response_pattern_default(self) -> None:
        template = parse_response_pattern("FA FB FC FD 00 {COM} FE FF")
        self.assertEqual(template, DEFAULT_RESPONSE_TEMPLATE)
        self.assertEqual(
            format_response_pattern(template), "FA FB FC FD 00 {COM} FE FF"
        )
        self.assertEqual(render_response(template, 5), build_response_frame(5))

    def test_parse_response_pattern_requires_single_slot(self) -> None:
        with self.assertRaises(ValueError):
            parse_response_pattern("")
        with self.assertRaises(ValueError):
            parse_response_pattern("FA FB FC FD 00 01 FE FF")
        with self.assertRaises(ValueError):
            parse_response_pattern("FA {COM} {COM} FE FF")
        with self.assertRaises(ValueError):
            parse_response_pattern("FA{COM}FEFF")

    def test_parse_response_pattern_supports_lowercase_slot(self) -> None:
        template = parse_response_pattern("AA55 {com} FEFF")
        self.assertEqual(template, (0xAA, 0x55, None, 0xFE, 0xFF))
        self.assertEqual(render_response(template, 2), b"\xAA\x55\x02\xFE\xFF")
        self.assertEqual(COM_PLACEHOLDER, "{COM}")

    def test_custom_template_parser(self) -> None:
        parser = FrameParser((0xAA, None, 0xBB))
        good = bytes([0xAA, 0x02, 0xBB])
        wrong = bytes([0xAA, 0x09, 0xBB])
        self.assertEqual(parser.feed(b"\x01" + good), [good])
        # 插槽是通配字节，错误 COM 号也会被识别为完整帧，交由判定逻辑比较
        self.assertEqual(parser.feed(wrong), [wrong])

    def test_empty_template_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FrameParser(())


if __name__ == "__main__":
    unittest.main()
