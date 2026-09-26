import argparse
from pathlib import Path

from kvmcp.capture import MODES


def main() -> None:
    parser = argparse.ArgumentParser(prog='kvmcp')
    parser.add_argument('--cache-dir', type=Path, default=Path('/tmp/kvmcp'), help='缓存目录（默认 %(default)s）')
    parser.add_argument('--capture-device', default='UGREEN', help='视频采集设备（默认 %(default)s）')
    parser.add_argument('--capture-mode', default='1080p', choices=sorted(MODES), help='视频采集模式（默认 %(default)s）')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址（默认 %(default)s）')
    parser.add_argument('--port', type=int, default=9527, help='监听端口（默认 %(default)s）')
    args = parser.parse_args()

    from kvmcp.server import serve

    serve(args.cache_dir, args.host, args.port, args.capture_device, args.capture_mode)
