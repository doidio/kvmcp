"""Discover a compatible video input and keep its newest PNG available."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import tomlkit

log = logging.getLogger(__name__)

VIDEO_SIZES = ('1920x1080', '2560x1440')
FRAME_TIMEOUT = 8.0
POLL_INTERVAL = 0.25
PIXEL_FORMATS = {'yuyv422': 'YUYV', 'mjpeg': 'MJPG'}
INPUT_FORMATS = tuple(PIXEL_FORMATS)
FORMAT_RE = re.compile(r"\[\d+\]:\s*'([^']+)'")
SIZE_RE = re.compile(r'Size:\s*Discrete\s+(\d+x\d+)')
FPS_RE = re.compile(r'\((\d+(?:\.\d+)?) fps\)')


@dataclass(frozen=True)
class Frame:
    data: bytes
    age: float
    video_size: str
    input_format: str


def _run_v4l2(*args: str) -> str:
    result = subprocess.run(['v4l2-ctl', *args], capture_output=True, text=True, timeout=5, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'v4l2-ctl 退出 {result.returncode}')
    return result.stdout


def _video_nodes(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip().startswith('/dev/video')]


def _is_video_capture(info: str) -> bool:
    """Use per-node device capabilities, not card-wide capabilities."""
    section = info.split('Device Caps', 1)
    if len(section) != 2:
        return False
    capabilities = []
    for line in section[1].splitlines()[1:]:
        if not line.startswith((' ', '\t')):
            break
        capabilities.append(line.strip())
    return 'Video Capture' in capabilities


def _supports_mode(formats: str, pixel_format: str, video_size: str, framerate: float) -> bool:
    """Match format and size; ffmpeg negotiates the exact frame interval."""
    fourcc = PIXEL_FORMATS.get(pixel_format, pixel_format.upper())
    current_format = None
    matching_size = False
    frame_rates: list[float] = []

    def supported() -> bool:
        return matching_size and (not frame_rates or max(frame_rates) >= framerate)

    for line in formats.splitlines():
        if match := FORMAT_RE.search(line):
            if current_format == fourcc and supported():
                return True
            current_format = match.group(1)
            matching_size = False
            frame_rates = []
        elif match := SIZE_RE.search(line):
            if current_format == fourcc and supported():
                return True
            matching_size = current_format == fourcc and match.group(1) == video_size
            frame_rates = []
        elif matching_size and (match := FPS_RE.search(line)):
            frame_rates.append(float(match.group(1)))
    return current_format == fourcc and supported()


def find_capture_devices(input_format: str, video_size: str, framerate: float) -> list[str]:
    """Find matching image capture nodes without depending on a device brand."""
    candidates = []
    for node in _video_nodes(_run_v4l2('--list-devices')):
        try:
            if _is_video_capture(_run_v4l2('-d', node, '--info')) and _supports_mode(
                _run_v4l2('-d', node, '--list-formats-ext'), input_format, video_size, framerate
            ):
                candidates.append(node)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            log.warning('Unable to inspect %s: %s', node, exc)
    return candidates


def _save_capture_setting(config_path: Path, key: str, value: str) -> None:
    """Preserve TOML comments and replace the configuration atomically."""
    document = tomlkit.parse(config_path.read_text(encoding='utf-8'))
    document['capture'][key] = value
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config_path.parent, prefix=f'.{config_path.name}.', delete=False) as file:
            temporary = Path(file.name)
            file.write(tomlkit.dumps(document))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, config_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Capturer:
    """Own a background supervisor; only it starts and stops ffmpeg."""

    def __init__(self, cfg: dict, config_path: Path) -> None:
        capture = cfg['capture']
        video_size = capture['video_size']
        if video_size not in VIDEO_SIZES:
            raise ValueError(f'不支持的分辨率: {video_size}')
        input_format = str(capture['input_format'])
        if input_format not in INPUT_FORMATS:
            raise ValueError(f'不支持的视频格式: {input_format}')
        self.framerate = float(capture['framerate'])
        if not 0 < self.framerate < 1000:
            raise ValueError('framerate 必须大于 0 且小于 1000')

        self.config_path = config_path.resolve()
        self.capture_dir = Path(cfg['cache_dir']) / 'capture'
        self._desired_size = video_size
        self._desired_format = input_format
        self._revision = 0
        self._active_file: Path | None = None
        self._active_size: str | None = None
        self._active_format: str | None = None
        self._status = '采集未启动'
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._supervise, name='kvmcp-ffmpeg', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=8)
            if self._thread.is_alive():
                log.error('Capture supervisor did not stop within 8 seconds')
        with self._lock:
            self._active_file = None
            self._active_size = None
            self._active_format = None
            self._status = '采集已停止'

    def set_video_size(self, video_size: str) -> None:
        if video_size not in VIDEO_SIZES:
            raise ValueError(f'不支持的分辨率: {video_size}')
        self._set_capture_setting('video_size', video_size)

    def set_input_format(self, input_format: str) -> None:
        if input_format not in INPUT_FORMATS:
            raise ValueError(f'不支持的视频格式: {input_format}')
        self._set_capture_setting('input_format', input_format)

    def _set_capture_setting(self, key: str, value: str) -> None:
        with self._lock:
            current = self._desired_size if key == 'video_size' else self._desired_format
            if value == current:
                return
            _save_capture_setting(self.config_path, key, value)
            if key == 'video_size':
                self._desired_size = value
            else:
                self._desired_format = value
            self._revision += 1
            self._active_file = None
            self._active_size = None
            self._active_format = None
            self._status = '正在切换采集分辨率' if key == 'video_size' else '正在切换采集格式'
            self._wake.set()

    def latest(self) -> Frame | str:
        with self._lock:
            path = self._active_file
            video_size = self._active_size
            input_format = self._active_format
            revision = self._revision
            status = self._status
        if path is None or video_size is None or input_format is None:
            return status
        try:
            with path.open('rb') as file:
                data = file.read()
                modified = os.fstat(file.fileno()).st_mtime
        except OSError:
            return status
        with self._lock:
            if revision != self._revision or path != self._active_file:
                return self._status
        if not data:
            return status
        age = max(0.0, time.time() - modified)
        if age > FRAME_TIMEOUT:
            return '无画面：画面未更新'
        return Frame(data, age, video_size, input_format)

    def _snapshot(self) -> tuple[str, str, int]:
        with self._lock:
            return self._desired_format, self._desired_size, self._revision

    def _set_status(self, status: str, revision: int) -> None:
        with self._lock:
            if revision == self._revision:
                self._status = status
                self._active_file = None
                self._active_size = None
                self._active_format = None

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _supervise(self) -> None:
        try:
            with ExitStack() as files:
                try:
                    log_file = files.enter_context((self.capture_dir / 'ffmpeg.log').open('ab', buffering=0))
                except OSError:
                    log.warning('Unable to open ffmpeg.log; continuing without a capture log', exc_info=True)
                    log_file = files.enter_context(Path(os.devnull).open('ab'))
                while not self._stop.is_set():
                    input_format, video_size, revision = self._snapshot()
                    try:
                        candidates = find_capture_devices(input_format, video_size, self.framerate)
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        self._set_status(f'采集异常：设备扫描失败：{exc}', revision)
                        log.exception('Capture device scan failed')
                        self._wait(1)
                        continue
                    if not candidates:
                        self._set_status('无符合当前采集模式的设备', revision)
                        self._wait(1)
                        continue
                    for device in candidates:
                        if self._stop.is_set() or revision != self._snapshot()[2]:
                            break
                        self._run_device(device, input_format, video_size, revision, log_file)
                    self._wait(1)
        except OSError as exc:
            log.exception('Capture supervisor failed')
            with self._lock:
                self._active_file = None
                self._active_size = None
                self._active_format = None
                self._status = f'采集异常：后台任务已停止：{exc}'

    def _run_device(self, device: str, input_format: str, video_size: str, revision: int, log_file: BinaryIO) -> None:
        path = self.capture_dir / f'frame-{uuid.uuid4().hex}.png'
        with self._lock:
            self._active_file = path
            self._active_size = video_size
            self._active_format = input_format
            self._status = '等待新画面'
        try:
            process = subprocess.Popen(
                [
                    'ffmpeg',
                    '-y',
                    '-hide_banner',
                    '-loglevel',
                    'error',
                    '-f',
                    'v4l2',
                    '-input_format',
                    input_format,
                    '-video_size',
                    video_size,
                    '-framerate',
                    str(self.framerate),
                    '-i',
                    device,
                    '-vf',
                    'fps=1',
                    '-f',
                    'image2',
                    '-update',
                    '1',
                    '-atomic_writing',
                    '1',
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                start_new_session=True,
            )
        except OSError as exc:
            self._set_status(f'采集异常：ffmpeg 启动失败：{exc}', revision)
            log.exception('Unable to start ffmpeg on %s', device)
            return

        last_modified = 0.0
        last_frame_at = time.monotonic()
        failure = '无画面'
        try:
            while not self._stop.is_set() and revision == self._snapshot()[2]:
                if (code := process.poll()) is not None:
                    failure = f'采集异常：ffmpeg 退出 {code}'
                    break
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    modified = 0.0
                if modified > last_modified:
                    last_modified = modified
                    last_frame_at = time.monotonic()
                    with self._lock:
                        if revision == self._revision:
                            self._status = '采集中'
                elif time.monotonic() - last_frame_at > FRAME_TIMEOUT:
                    failure = '无画面：等待新帧超时'
                    break
                self._wait(POLL_INTERVAL)
        finally:
            self._set_status(failure, revision)
            _stop_process(process)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning('Unable to remove old frame %s', path, exc_info=True)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.error('ffmpeg did not exit after SIGKILL')
    except ProcessLookupError:
        pass
