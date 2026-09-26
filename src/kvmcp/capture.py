"""Keep one ffmpeg process writing the latest frame as a PNG."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

CAPTURE_DIR = 'capture'
FRAME_NAME = 'latest.png'

MODES = {
    '1080p': {'input_format': 'yuyv422', 'video_size': '1920x1080', 'framerate': '5', 'output_fps': '1'},
    '1440p': {'input_format': 'yuyv422', 'video_size': '2560x1440', 'framerate': '5', 'output_fps': '1'},
}


def video_nodes_for_card(list_output: str, name: str) -> list[str]:
    """Return /dev/video nodes listed under a matching v4l2 card."""
    nodes: list[str] = []
    current = False
    for line in list_output.splitlines():
        if line.startswith((' ', '\t')):
            path = line.strip()
            if current and path.startswith('/dev/video'):
                nodes.append(path)
            continue
        current = name in line
    return nodes


def is_video_capture(info: str) -> bool:
    """True when Device Caps includes Video Capture and not Metadata Capture."""
    collecting = False
    caps: list[str] = []
    for line in info.splitlines():
        if 'Device Caps' in line:
            collecting = True
            caps = []
            continue
        if not collecting:
            continue
        if not line.startswith((' ', '\t')):
            break
        caps.append(line.strip())
    return 'Video Capture' in caps and 'Metadata Capture' not in caps


def find_capture_device(device: str) -> str | None:
    """Pick the capture node whose v4l2 name matches. Nodes move after replug."""
    listed = subprocess.run(
        ['v4l2-ctl', '--list-devices'],
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        log.warning('v4l2-ctl --list-devices failed: %s', listed.stderr.strip())
        return None
    for node in video_nodes_for_card(listed.stdout, device):
        info = subprocess.run(
            ['v4l2-ctl', '-d', node, '--info'],
            check=False,
            capture_output=True,
            text=True,
        )
        if info.returncode == 0 and is_video_capture(info.stdout):
            return node
    return None


class Capture:
    """ffmpeg subprocess whose lifetime follows this object."""

    def __init__(self, device: str, cache_dir: Path, mode: str) -> None:
        self.device = device
        self.cache_dir = cache_dir
        self.capture_dir = cache_dir / CAPTURE_DIR
        self.mode = MODES[mode]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._started_at: float | None = None

    def start(self) -> None:
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._supervise, name='kvmcp-ffmpeg', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def latest(self) -> tuple[Path, float] | None:
        """Return the current frame and seconds since this capture session started."""
        path = self.capture_dir / FRAME_NAME
        if not _nonempty(path):
            return None
        with self._lock:
            started_at = self._started_at
        if started_at is None:
            return None
        elapsed = path.stat().st_mtime - started_at
        if elapsed < 0:
            return None
        return path, elapsed

    def _supervise(self) -> None:
        with (self.capture_dir / 'ffmpeg.log').open('ab', buffering=0) as log_file:
            while not self._stop.is_set():
                device = find_capture_device(self.device)
                if device is None:
                    log.info('capture device not found')
                    self._stop.wait(1)
                    continue
                log.info('starting ffmpeg on %s', device)
                proc = self._spawn(device, log_file)
                with self._lock:
                    self._proc = proc
                while not self._stop.is_set() and proc.poll() is None:
                    self._stop.wait(1)
                self._terminate()
                if not self._stop.is_set():
                    log.info('ffmpeg exited, restarting')
                    self._stop.wait(1)

    def _spawn(self, device: str, log_file: BinaryIO) -> subprocess.Popen[bytes]:
        with self._lock:
            self._started_at = time.time()
        return subprocess.Popen(
            [
                'ffmpeg',
                '-y',
                '-hide_banner',
                '-loglevel',
                'error',
                '-f',
                'v4l2',
                '-input_format',
                self.mode['input_format'],
                '-video_size',
                self.mode['video_size'],
                '-framerate',
                self.mode['framerate'],
                '-i',
                device,
                '-vf',
                f'fps={self.mode["output_fps"]}',
                '-f',
                'image2',
                '-update',
                '1',
                '-atomic_writing',
                '1',
                str(self.capture_dir / FRAME_NAME),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )

    def _terminate(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False
