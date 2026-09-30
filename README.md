# kvmcp

> 安全内网 MCP 工具，连接本地 VLM 大模型自主操控目标主机

## 开发板选型

> ⚠️ 短板 ⭐ 优势 ✅ 已实测 ❓ 未实测 

| 项目 | [Raspberry Pi Zero 2 W](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) | [Orange Pi Zero 2W](http://www.orangepi.cn/html/hardWare/computerAndMicrocontrollers/service-and-support/Orange-Pi-Zero-2W.html) | [Radxa ZERO 3W](https://docs.radxa.com/zero/zero3) |
|---|---|---|---|
| 参考价格 | 899 元 | 415 元 | 299 元 |
| 处理器 | 4× Cortex-A53，1 GHz | 4× Cortex-A53，最高 1.5 GHz | 4× Cortex-A55，最高 1.6 GHz |
| 内存 | ⚠️ 512 MB LPDDR2 | 2 GB LPDDR4 | 2 GB LPDDR4 |
| 无线网络 | ⚠️ 2.4 GHz Wi-Fi 4、蓝牙 4.2 | Wi-Fi 5、蓝牙 5.0 | Wi-Fi 6、蓝牙 5.4 |
| 独立 USB Host | ⚠️ 无 | USB1：USB 2.0 Type-C | USB 3.0 Type-C ⭐ |
| USB Device | Micro-USB OTG，HID Gadget | USB0：USB 2.0 Type-C，HID Gadget ✅ | USB 2.0 Type-C OTG，HID Gadget ❓ |
| HID 兼供电 | 支持 | 稳定支持 ✅ | 稳定支持 ❓ |
| 存储 | microSD | microSD | microSD + eMMC |
| 视频输出 | Mini HDMI | Mini HDMI | Micro HDMI |
| 扩展接口 | 40-pin GPIO | 40-pin GPIO + 24-pin 专用扩展板 | 40-pin GPIO + MIPI CSI |
| 尺寸 | 65 × 30 mm | 65 × 30 mm | 65 × 30 mm |

## 安装与运行

#### 安装系统依赖

```sh
sudo apt update
sudo apt install -y ffmpeg v4l-utils
```

#### 安装 [uv python](https://docs.astral.sh/uv/getting-started/installation/) 环境

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### 安装 kvmcp

```sh
git clone https://github.com/doidio/kvmcp.git
cd kvmcp
uv sync
```

#### 安装系统服务

```sh
KVMCP_USB_VID=0x1d6b KVMCP_USB_PID=0x0104 bash scripts/setup-kvmcp.sh
systemctl status kvmcp-hid kvmcp
```

- 示例 VID/PID 仅供开发测试，非 kvmcp 专属
- 开发板须支持 configfs HID Gadget
- 默认绝对鼠标模式，备用相对鼠标模型 `KVMCP_MOUSE_MODE=relative`

#### HID 控制

`mouse_move(x, y)`、`mouse_click(x, y, button)` 使用最新采集画面的像素坐标，左上角 `(0, 0)`；当前支持绝对鼠标模式。`key_press(key, modifiers)` 按下并释放单个按键，可组合 `ctrl`、`shift`、`alt`、`meta`。采集画面失效时坐标操作会拒绝执行。部署脚本将对应 `/dev/hidg*` 节点授权给 `video` 组，供非 root 服务使用。

主机熄屏时可尝试发送鼠标移动指令，再重新获取画面确认；指令发送成功不保证画面恢复。采集服务本身只提供最新帧，等待后截图由 ChatKVM 执行。

这些 tools 会真实控制 USB 所接电脑；当前服务监听 `0.0.0.0` 且没有身份验证，只应运行在可信、隔离的网络中。

#### 接入 ChatKVM

在 ChatKVM 的 `config.toml` 中添加设备：

```toml
[kvmcp.office_pc]
url = "http://<开发板IP>:9527/mcp"
description = "办公室电脑"
```

kvmcp 提供单台设备的采集和 HID 接口；ChatKVM 通过 MCP 客户端调用，并向 Agent 提供统一的多设备工具。设备选择、观察和操作核验由 ChatKVM 管理。鼠标参数使用采集原图的像素坐标，按键参数使用物理键名及可选修饰键。

## Q&A

#### Why not [PiKVM](https://pikvm.org/)

PiKVM 面向人类远程操作，采用 usteamer 流送视频，确保画面流畅，而 kvmcp 面向 VLM 大模型，采用无损保真的 PNG 截图，确保准确识别画面中的 UI 元素和文字；也不需要独立电源，可用 USB 模拟 HID 兼供电，随目标主机开关
