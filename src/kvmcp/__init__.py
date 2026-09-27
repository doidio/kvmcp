import argparse
from pathlib import Path

import tomlkit

from kvmcp.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(prog='kvmcp')
    parser.add_argument('-c', '--config', default='config.toml')
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = tomlkit.loads(config_path.read_text(encoding='utf-8')).unwrap()
    serve(cfg, config_path)
