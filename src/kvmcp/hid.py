"""Write keyboard and absolute-mouse reports to the Linux USB HID gadget."""

from __future__ import annotations

import os
import select
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

GADGET = Path('/sys/kernel/config/usb_gadget/kvmcp')
HID_REPORT_MAX = 32767
BUTTONS = {'left': 1, 'right': 2, 'middle': 4}
MODIFIERS = {'ctrl': 0x01, 'shift': 0x02, 'alt': 0x04, 'meta': 0x08}
KEYS = {
    **{chr(ord('a') + i): 0x04 + i for i in range(26)},
    **{str((i + 1) % 10): 0x1E + i for i in range(10)},
    'enter': 0x28,
    'escape': 0x29,
    'backspace': 0x2A,
    'tab': 0x2B,
    'space': 0x2C,
    'delete': 0x4C,
    'right': 0x4F,
    'left': 0x50,
    'down': 0x51,
    'up': 0x52,
    'home': 0x4A,
    'end': 0x4D,
    'pageup': 0x4B,
    'pagedown': 0x4E,
    **{f'f{i}': 0x3A + i - 1 for i in range(1, 13)},
}


def pixel_to_hid(value: int, extent: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or extent < 2 or not 0 <= value < extent:
        raise ValueError(f'坐标 {value} 超出 0..{extent - 1}')
    return (value * HID_REPORT_MAX + (extent - 1) // 2) // (extent - 1)


def mouse_report(buttons: int, x: int, y: int) -> bytes:
    return bytes((buttons,)) + x.to_bytes(2, 'little') + y.to_bytes(2, 'little') + b'\0'


def keyboard_report(key: str, modifiers: list[str]) -> bytes:
    key = key.lower()
    if key not in KEYS:
        raise ValueError(f'不支持的按键: {key}')
    mask = 0
    for name in modifiers:
        name = name.lower()
        if name not in MODIFIERS:
            raise ValueError(f'不支持的修饰键: {name}')
        mask |= MODIFIERS[name]
    return bytes((mask, 0, KEYS[key], 0, 0, 0, 0, 0))


def _device_path(function: str) -> Path:
    """Resolve the node by its device number; /dev/hidg numbering is not fixed."""
    if not (GADGET / 'UDC').read_text().strip():
        raise OSError('USB HID Gadget 尚未绑定')
    function_dir = GADGET / 'functions' / f'hid.{function}'
    major, minor = map(int, (function_dir / 'dev').read_text().strip().split(':'))
    target = os.makedev(major, minor)
    for node in Path('/dev').glob('hidg*'):
        try:
            info = node.stat()
        except FileNotFoundError:
            continue
        if stat.S_ISCHR(info.st_mode) and info.st_rdev == target:
            return node
    raise OSError(f'未找到 hid.{function} 对应的 /dev/hidg 设备')


def _write(fd: int, report: bytes) -> None:
    deadline = time.monotonic() + 1.0
    while True:
        if not select.select([], [fd], [], max(0, deadline - time.monotonic()))[1]:
            raise TimeoutError('USB HID 设备写入超时')
        try:
            if os.write(fd, report) != len(report):
                raise OSError('USB HID 报告写入不完整')
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError('USB HID 设备写入超时') from None


class HIDController:
    """Serialize complete HID gestures so concurrent MCP calls cannot interleave."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def mouse_move(self, x: int, y: int, width: int, height: int) -> None:
        report = mouse_report(0, pixel_to_hid(x, width), pixel_to_hid(y, height))
        with self._lock:
            with _open_device('mouse') as fd:
                _write(fd, report)

    def mouse_click(self, x: int, y: int, width: int, height: int, button: str = 'left') -> None:
        if button not in BUTTONS:
            raise ValueError(f'不支持的鼠标按钮: {button}')
        hx, hy = pixel_to_hid(x, width), pixel_to_hid(y, height)
        released = mouse_report(0, hx, hy)
        pressed = mouse_report(BUTTONS[button], hx, hy)
        with self._lock:
            with _open_device('mouse') as fd:
                _write(fd, released)
                try:
                    _write(fd, pressed)
                    time.sleep(0.03)
                finally:
                    _write(fd, released)

    def key_press(self, key: str, modifiers: list[str] | None = None) -> None:
        pressed = keyboard_report(key, modifiers or [])
        with self._lock:
            with _open_device('keyboard') as fd:
                _write(fd, b'\0' * 8)
                try:
                    _write(fd, pressed)
                    time.sleep(0.03)
                finally:
                    _write(fd, b'\0' * 8)

@contextmanager
def _open_device(function: str) -> Iterator[int]:
    if function == 'mouse' and (GADGET / 'functions/hid.mouse/report_length').read_text().strip() != '6':
        raise OSError('当前不是绝对鼠标模式；请用 KVMCP_MOUSE_MODE=absolute 配置 Gadget')
    fd = os.open(_device_path(function), os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)
