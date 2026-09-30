"""MCP server that returns the newest PNG ffmpeg has already saved."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from kvmcp.capture import INPUT_FORMATS, VIDEO_SIZES, Capturer
from kvmcp.hid import KEYS, HIDController

capturer: Capturer | None = None
hid = HIDController()


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    assert capturer is not None
    capturer.start()
    try:
        yield None
    finally:
        capturer.stop()


mcp = MCPServer(
    name='kvmcp',
    version='0.1.0',
    instructions='单台目标电脑的采集配置、PNG 截图和 USB HID 接口。鼠标使用采集图像的像素坐标，原点为左上角。HID 返回值表示报告发送状态。',
    lifespan=lifespan,
)


@mcp.tool(
    structured_output=False,
    description='返回最新采集的原尺寸 PNG 图像及尺寸、帧龄和采集格式',
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def latest_frame() -> tuple[str, Image]:
    assert capturer is not None
    frame = capturer.latest()
    if isinstance(frame, str):
        raise ToolError(frame)
    width, height = map(int, frame.video_size.split('x'))
    return (
        f'截图 {width}x{height} 像素；帧龄 {round(1000 * frame.age)} ms；采集格式 {frame.input_format}',
        Image(data=frame.data, format='png'),
    )


@mcp.tool(structured_output=False, description='设置采集分辨率并重启采集')
def set_video_size(video_size: Annotated[Literal[*VIDEO_SIZES], Field(description='采集分辨率，仅可使用列出的模式')]) -> None:
    if video_size not in VIDEO_SIZES:
        raise ToolError(f'不支持的分辨率: {video_size}, 支持: {", ".join(VIDEO_SIZES)}')

    assert capturer is not None
    try:
        capturer.set_video_size(video_size)
    except (OSError, ValueError, KeyError) as exc:
        raise ToolError(f'切换采集分辨率失败: {exc}') from exc


@mcp.tool(structured_output=False, description='设置 FFmpeg 输入格式并重启采集')
def set_input_format(input_format: Annotated[Literal[*INPUT_FORMATS], Field(description='FFmpeg 的输入格式名，仅可使用列出的格式')]) -> None:
    if input_format not in INPUT_FORMATS:
        raise ToolError(f'不支持的视频格式: {input_format}, 支持: {", ".join(INPUT_FORMATS)}')

    assert capturer is not None
    try:
        capturer.set_input_format(input_format)
    except (OSError, ValueError, KeyError) as exc:
        raise ToolError(f'切换采集视频格式失败: {exc}') from exc


def _frame_dimensions() -> tuple[int, int]:
    assert capturer is not None
    frame = capturer.latest()
    if isinstance(frame, str):
        raise ToolError(f'无法定位鼠标：{frame}')
    return tuple(map(int, frame.video_size.split('x')))


@mcp.tool(structured_output=False, description='按当前采集图像的像素坐标移动鼠标指针')
def mouse_move(
    x: Annotated[int, Field(ge=0, description='水平像素坐标，左边缘为 0')],
    y: Annotated[int, Field(ge=0, description='垂直像素坐标，上边缘为 0')],
) -> str:
    width, height = _frame_dimensions()
    try:
        hid.mouse_move(x, y, width, height)
    except (OSError, TimeoutError, ValueError) as exc:
        raise ToolError(f'鼠标移动失败: {exc}') from exc
    return f'已发送鼠标移动到 ({x}, {y})。'


@mcp.tool(structured_output=False, description='在当前采集图像的像素坐标单击一次，依次移动、按下并释放鼠标')
def mouse_click(
    x: Annotated[int, Field(ge=0, description='水平像素坐标，左边缘为 0')],
    y: Annotated[int, Field(ge=0, description='垂直像素坐标，上边缘为 0')],
    button: Annotated[Literal['left', 'right', 'middle'], Field(description='鼠标按键：左键、右键或中键')] = 'left',
) -> str:
    width, height = _frame_dimensions()
    try:
        hid.mouse_click(x, y, width, height, button)
    except (OSError, TimeoutError, ValueError) as exc:
        raise ToolError(f'鼠标单击失败: {exc}') from exc
    return f'已发送 {button} 单击 ({x}, {y})。'


@mcp.tool(structured_output=False, description='按下并释放一个物理按键及可选修饰键')
def key_press(
    key: Annotated[Literal[*tuple(KEYS)], Field(description='单个 HID 键名；字母和数字用小写，功能键使用枚举值')],
    modifiers: Annotated[
        list[Literal['ctrl', 'shift', 'alt', 'meta']] | None, Field(description='同时按下的修饰键，例如 ["ctrl", "shift"]；不需要时省略')
    ] = None,
) -> str:
    try:
        hid.key_press(key, modifiers)
    except (OSError, TimeoutError, ValueError) as exc:
        raise ToolError(f'按键失败: {exc}') from exc
    return f'已发送按键 {"+".join([*(modifiers or []), key])}。'


def serve(cfg: dict, config_path: Path) -> None:
    global capturer
    capturer = Capturer(cfg, config_path)

    mcp.run(transport='streamable-http', host=cfg['server']['host'], port=cfg['server']['port'], json_response=True)
