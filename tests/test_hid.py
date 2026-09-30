from contextlib import nullcontext
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from kvmcp.capture import Frame
from kvmcp.hid import HIDController, keyboard_report, mouse_report, pixel_to_hid
from kvmcp import server


class ReportTests(unittest.TestCase):
    def test_pixel_coordinates_cover_full_absolute_range(self):
        self.assertEqual(pixel_to_hid(0, 1920), 0)
        self.assertEqual(pixel_to_hid(1919, 1920), 32767)
        self.assertEqual(pixel_to_hid(2559, 2560), 32767)
        self.assertAlmostEqual(pixel_to_hid(960, 1920), 16384, delta=10)
        for x in (-1, 1920, True):
            with self.subTest(x=x), self.assertRaises(ValueError):
                pixel_to_hid(x, 1920)

    def test_absolute_mouse_report_matches_gadget_descriptor(self):
        self.assertEqual(mouse_report(1, 32767, 0), b'\x01\xff\x7f\0\0\0')

    def test_keyboard_report_and_validation(self):
        self.assertEqual(keyboard_report('a', ['ctrl', 'shift']), b'\x03\0\x04\0\0\0\0\0')
        self.assertEqual(keyboard_report('0', []), b'\0\0\x27\0\0\0\0\0')
        with self.assertRaises(ValueError):
            keyboard_report('nonexistent', [])
        with self.assertRaises(ValueError):
            keyboard_report('a', ['nonexistent'])


class GestureTests(unittest.TestCase):
    def setUp(self):
        self.controller = HIDController()
        self.reports = []
        opener = patch('kvmcp.hid._open_device', return_value=nullcontext(12))
        writer = patch('kvmcp.hid._write', side_effect=lambda _fd, report: self.reports.append(report))
        sleeper = patch('kvmcp.hid.time.sleep')
        opener.start()
        writer.start()
        sleeper.start()
        self.addCleanup(opener.stop)
        self.addCleanup(writer.stop)
        self.addCleanup(sleeper.stop)

    def test_click_moves_presses_and_releases_at_pixel_position(self):
        self.controller.mouse_click(1919, 1079, 1920, 1080)
        self.assertEqual(self.reports, [mouse_report(0, 32767, 32767), mouse_report(1, 32767, 32767), mouse_report(0, 32767, 32767)])

    def test_keyboard_is_released(self):
        self.controller.key_press('tab', ['shift'])
        self.assertEqual(self.reports, [bytes(8), keyboard_report('tab', ['shift']), bytes(8)])

    def test_invalid_coordinate_produces_no_report(self):
        with self.assertRaises(ValueError):
            self.controller.mouse_move(1920, 0, 1920, 1080)
        self.assertEqual(self.reports, [])


class ToolTests(unittest.TestCase):
    def test_mouse_uses_current_capture_dimensions(self):
        class Capture:
            def latest(self):
                return Frame(b'png', 0, '2560x1440', 'yuyv422')

        with patch.object(server, 'capturer', Capture()), patch.object(server.hid, 'mouse_move') as move:
            server.mouse_move(2559, 1439)
        move.assert_called_once_with(2559, 1439, 2560, 1440)

    def test_mouse_rejects_missing_capture(self):
        class Capture:
            def latest(self):
                return '无画面'

        with patch.object(server, 'capturer', Capture()), patch.object(server.hid, 'mouse_move') as move:
            with self.assertRaises(ToolError):
                server.mouse_move(0, 0)
        move.assert_not_called()


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_schema_uses_image_pixels_and_enumerated_keys(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertNotIn('归一化', server.mcp.instructions)
        self.assertIn('像素坐标', server.mcp.instructions)
        self.assertEqual(tools['mouse_move'].input_schema['properties']['x']['minimum'], 0)
        self.assertEqual(tools['mouse_move'].input_schema['properties']['y']['type'], 'integer')
        self.assertIn('escape', tools['key_press'].input_schema['properties']['key']['enum'])
        self.assertEqual(tools['mouse_click'].input_schema['properties']['button']['default'], 'left')
        self.assertTrue(tools['latest_frame'].annotations.read_only_hint)
        self.assertNotIn('wake_display', tools)


if __name__ == '__main__':
    unittest.main()
