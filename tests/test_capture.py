import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kvmcp.capture import Capturer, Frame, _is_video_capture, _supports_mode, find_capture_devices

CAPTURE_INFO = """Driver Info:
\tDriver name      : uvcvideo
\tDevice Caps      : 0x04200001
\t\tVideo Capture
\t\tStreaming
"""
METADATA_INFO = """Driver Info:
\tDriver name      : uvcvideo
\tDevice Caps      : 0x04a00000
\t\tMetadata Capture
\t\tStreaming
"""
FORMATS = """[0]: 'MJPG' (Motion-JPEG)
\tSize: Discrete 1920x1080
\t\tInterval: Discrete 0.033s (30.000 fps)
[1]: 'YUYV' (YUYV 4:2:2)
\tSize: Discrete 1920x1080
\t\tInterval: Discrete 0.200s (5.000 fps)
\tSize: Discrete 2560x1440
\t\tInterval: Discrete 0.200s (5.000 fps)
"""


class DiscoveryTests(unittest.TestCase):
    def test_capture_capabilities_and_mode(self):
        self.assertTrue(_is_video_capture(CAPTURE_INFO))
        self.assertFalse(_is_video_capture(METADATA_INFO))
        self.assertTrue(_is_video_capture(CAPTURE_INFO.replace('Streaming', 'Metadata Capture\n\t\tStreaming')))
        self.assertTrue(_supports_mode(FORMATS, 'yuyv422', '2560x1440', 5))
        self.assertFalse(_supports_mode(FORMATS, 'yuyv422', '2560x1440', 10))
        self.assertFalse(_supports_mode(FORMATS, 'mjpeg', '2560x1440', 5))

    def test_finds_compatible_nodes_without_brand(self):
        def command(*args):
            if args == ('--list-devices',):
                return 'Card A:\n\t/dev/video0\n\t/dev/video1\nCard B:\n\t/dev/video2\n'
            node = args[1]
            if args[2] == '--info':
                return METADATA_INFO if node == '/dev/video1' else CAPTURE_INFO
            return FORMATS if node == '/dev/video2' else "[0]: 'MJPG'\n\tSize: Discrete 640x480\n"

        with patch('kvmcp.capture._run_v4l2', side_effect=command):
            self.assertEqual(find_capture_devices('yuyv422', '1920x1080', 5), ['/dev/video2'])


class CapturerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('[capture]\n# keep me\nvideo_size = "1920x1080"\n', encoding='utf-8')
        self.cfg = {
            'cache_dir': str(self.root),
            'capture': {'video_size': '1920x1080', 'input_format': 'yuyv422', 'framerate': '5'},
        }
        self.capturer = Capturer(self.cfg, self.config)
        self.capturer.capture_dir.mkdir()

    def test_config_path_is_not_inserted_into_settings(self):
        self.assertEqual(self.capturer.config_path, self.config)
        self.assertNotIn('config_file', self.cfg)

    def test_old_frame_is_invalidated_on_disconnect(self):
        frame_path = self.capturer.capture_dir / 'frame.png'
        frame_path.write_bytes(b'png bytes')
        with self.capturer._lock:
            self.capturer._active_file = frame_path
            self.capturer._active_size = '1920x1080'
        frame = self.capturer.latest()
        self.assertIsInstance(frame, Frame)
        self.assertEqual(frame.data, b'png bytes')
        self.capturer._set_status('无设备', 0)
        self.assertEqual(self.capturer.latest(), '无设备')

    def test_switch_preserves_comments_and_invalidates_old_frame(self):
        frame_path = self.capturer.capture_dir / 'frame.png'
        frame_path.write_bytes(b'old frame')
        with self.capturer._lock:
            self.capturer._active_file = frame_path
            self.capturer._active_size = '1920x1080'
        self.capturer.set_video_size('2560x1440')
        self.assertIn('# keep me', self.config.read_text(encoding='utf-8'))
        self.assertIn('video_size = "2560x1440"', self.config.read_text(encoding='utf-8'))
        self.assertEqual(self.capturer.latest(), '正在切换采集分辨率')

    def test_failed_save_does_not_change_runtime_mode(self):
        with patch('kvmcp.capture._save_video_size', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.capturer.set_video_size('2560x1440')
        self.assertEqual(self.capturer._snapshot(), ('1920x1080', 0))

    def test_stale_frame_is_not_returned(self):
        frame_path = self.capturer.capture_dir / 'frame.png'
        frame_path.write_bytes(b'old frame')
        old = time.time() - 30
        os.utime(frame_path, (old, old))
        with self.capturer._lock:
            self.capturer._active_file = frame_path
            self.capturer._active_size = '1920x1080'
        self.assertEqual(self.capturer.latest(), '无画面：画面未更新')

    def test_switch_stops_process_owned_by_supervisor(self):
        launched = threading.Event()

        class Process:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 0

            def wait(self, timeout):
                return self.returncode

        process = Process()

        def spawn(*args, **kwargs):
            launched.set()
            return process

        with patch('kvmcp.capture.subprocess.Popen', side_effect=spawn):
            worker = threading.Thread(target=self.capturer._run_device, args=('/dev/video2', '1920x1080', 0, None))
            worker.start()
            self.assertTrue(launched.wait(2))
            self.capturer.set_video_size('2560x1440')
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.capturer.latest(), '正在切换采集分辨率')

    def test_supervisor_tries_next_compatible_device(self):
        attempted = []

        def run_device(device, *_args):
            attempted.append(device)
            if len(attempted) == 2:
                self.capturer._stop.set()

        with (
            patch('kvmcp.capture.find_capture_devices', return_value=['/dev/video0', '/dev/video2']),
            patch.object(self.capturer, '_run_device', side_effect=run_device),
            patch.object(self.capturer, '_wait'),
        ):
            self.capturer._supervise()
        self.assertEqual(attempted, ['/dev/video0', '/dev/video2'])

    def test_supervisor_recovers_from_scan_error(self):
        attempted = []

        def run_device(device, *_args):
            attempted.append(device)
            self.capturer._stop.set()

        with (
            patch('kvmcp.capture.find_capture_devices', side_effect=[OSError('temporary'), ['/dev/video2']]),
            patch.object(self.capturer, '_run_device', side_effect=run_device),
            patch.object(self.capturer, '_wait'),
            self.assertLogs('kvmcp.capture', level='ERROR'),
        ):
            self.capturer._supervise()
        self.assertEqual(attempted, ['/dev/video2'])


if __name__ == '__main__':
    unittest.main()
