"""MCP server that returns the newest PNG ffmpeg has already saved."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from kvmcp.capture import Capture

capture: Capture | None = None


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    assert capture is not None
    capture.start()
    try:
        yield None
    finally:
        capture.stop()


mcp = MCPServer(
    'kvmcp',
    instructions='调用 capture_frame 获取采集卡最近一张 PNG。编码在后台完成，这次调用只读取已写好的文件。',
    lifespan=lifespan,
)


@mcp.tool(
    structured_output=False,
    description='返回采集卡最近一张已经写好的 PNG，以及从本次采集开始到这帧写好的秒数，精确到毫秒。不重新打开设备，也不在这次调用里编码。',
)
def capture_frame() -> tuple[str, Image]:
    assert capture is not None
    frame = capture.latest()
    if frame is None:
        raise ToolError('还没有画面。采集刚启动，或采集卡未接上。')
    path, elapsed = frame
    return (f'{elapsed:.3f}', Image(path=path))


def serve(cache_dir: Path, host: str, port: int, device: str, mode: str) -> None:
    global capture
    capture = Capture(device, cache_dir, mode)
    mcp.run(transport='streamable-http', host=host, port=port, json_response=True)
