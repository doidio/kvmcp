"""MCP server that returns the newest PNG ffmpeg has already saved."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from kvmcp.capture import VIDEO_SIZES, Capturer

capturer: Capturer | None = None


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    assert capturer is not None
    capturer.start()
    try:
        yield None
    finally:
        capturer.stop()


mcp = MCPServer(name='kvmcp', version='0.1.0', lifespan=lifespan)


@mcp.tool(structured_output=False, description='查看最新屏幕画面')
def latest_frame() -> tuple[str, Image]:
    assert capturer is not None
    frame = capturer.latest()
    if isinstance(frame, str):
        raise ToolError(frame)
    return (f'`画面延迟 {frame.age:.3f} 秒` `{frame.video_size}` `{frame.input_format}`', Image(data=frame.data, format='png'))


@mcp.tool(structured_output=False, description='切换采集分辨率，可选 1920x1080 或 2560x1440')
def set_video_size(video_size: Literal[*VIDEO_SIZES]) -> None:
    if video_size not in VIDEO_SIZES:
        raise ToolError(f'不支持的分辨率: {video_size}, 支持: {", ".join(VIDEO_SIZES)}')

    assert capturer is not None
    try:
        capturer.set_video_size(video_size)
    except (OSError, ValueError, KeyError) as exc:
        raise ToolError(f'切换采集分辨率失败: {exc}') from exc


def serve(cfg: dict, config_path: Path) -> None:
    global capturer
    capturer = Capturer(cfg, config_path)

    mcp.run(transport='streamable-http', host=cfg['server']['host'], port=cfg['server']['port'], json_response=True)
