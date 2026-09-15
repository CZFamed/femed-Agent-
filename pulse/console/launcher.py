"""一键启动素材库控制台（面向非技术同事）。

双击仓库根目录的「一键启动_素材库控制台.bat」即会调用本模块：自动定位素材目录、
挑选空闲端口、打开浏览器，并用中文提示当前状态与容量预警。
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from pulse import __version__ as PULSE_VERSION
from pulse.console.media_console import MediaConsoleApp, create_server
from pulse.services.media.config import BRAND_NAME

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LAUNCHER_BAT_NAME = "一键启动_素材库控制台.bat"
SHORTCUT_NAME = "Pulse素材库"
RAG_RELATIVE = Path("RAG知识库") / "图片描述"
MEDIA_RELATIVE = Path("..") / "菲美得产品图片"
LEDGER_RELATIVE = Path(".pulse") / "media_ledger.sqlite3"
RULE = "=" * 62


def project_root() -> Path:
    """仓库根目录（``pulse/console/launcher.py`` 上溯三级）。"""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ConsolePaths:
    """启动所需的三个路径。"""

    rag_root: Path
    media_root: Path
    ledger_path: Path
    root: Path | None = None
    missing: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.missing


def resolve_paths(
    root: str | Path | None = None,
    *,
    rag_root: str | Path | None = None,
    media_root: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> ConsolePaths:
    """解析素材目录；返回缺失项用于中文提示。"""
    base = Path(root) if root else project_root()
    rag = Path(rag_root) if rag_root else base / RAG_RELATIVE
    media = Path(media_root) if media_root else base / MEDIA_RELATIVE
    ledger = Path(ledger_path) if ledger_path else base / LEDGER_RELATIVE
    missing = tuple(
        label
        for label, path in (("RAG 图片描述库（RAG知识库/图片描述）", rag), ("实拍素材目录（../菲美得产品图片）", media))
        if not path.is_dir()
    )
    return ConsolePaths(
        rag_root=rag, media_root=media, ledger_path=ledger, root=base, missing=missing
    )


def pick_port(preferred: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> int:
    """优先用 preferred；**确实被人占着**时由系统分配一个空闲端口。

    ⚠ 不能只靠 bind 试探：Windows 上先启动的服务带 ``SO_REUSEADDR``，
    第二个进程照样能 bind 成功——2026-09-15 就是这么出现"两个控制台同时监听
    8765"的，页面命中哪个全看运气，于是"代码改了但页面还是旧的"。
    所以先 connect 一次：连得上就说明真有人占着，直接换端口。
    """
    if not _has_listener(host, preferred):
        with socket.socket() as probe:
            try:
                probe.bind((host, preferred))
                return int(probe.getsockname()[1])
            except OSError:
                pass
    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _open_browser(url: str) -> None:
    """打开浏览器；打不开只提示，不影响服务。"""
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # pragma: no cover - 浏览器不可用不影响服务
        print("浏览器未自动打开，请手动复制上面的地址到浏览器。")


def _has_listener(host: str, port: int, *, timeout: float = 0.4) -> bool:
    """该端口上是否已经有人在监听（connect 成功即视为占用）。"""
    if port <= 0:
        return False
    with socket.socket() as probe:
        probe.settimeout(timeout)
        try:
            probe.connect((host, port))
        except OSError:
            return False
    return True


def probe_running_console(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, timeout: float = 1.5
) -> str | None:
    """看这个端口上是不是已经跑着我们自己的控制台；是就返回它的版本号。

    返回 ``None`` 表示端口上没有人，或者占着它的是别的程序（不能乱认）。
    老版本控制台的 ``/api/state`` 里没有 ``version`` 字段，会返回空字符串——
    这正好用来提示"旧窗口还开着"。
    """
    if port <= 0:
        return None
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/state", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(payload, dict) or "capacity" not in payload or "brand" not in payload:
        return None
    return str(payload.get("version") or "")


def plan_start(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    current_version: str = PULSE_VERSION,
    probe=probe_running_console,
) -> tuple[bool, int, str]:
    """决定"复用已有控制台"还是"另起一个"。

    返回 ``(reuse, port, message)``：

    - 端口空着 → ``(False, port, "")``，照常启动；
    - 端口上已经是**同一版**控制台 → ``(True, port, 提示)``：只打开浏览器，
      不再起第二个进程（双击两次不会留下两个旧进程）；
    - 端口上是**旧版**控制台 → ``(False, 0, 提示)``：在新端口启动最新版，
      并明确告诉用户把旧窗口关掉。
    """
    running = probe(host, port)
    if running is None:
        return False, port, ""
    if running == current_version:
        return (
            True,
            port,
            f"素材库控制台已经在运行（端口 {port}，版本 {running}）：已直接打开页面，"
            "不用再开第二个窗口。",
        )
    shown = running or "未知（老版本没有版本号）"
    return (
        False,
        0,
        f"检测到旧版本控制台还开着（版本 {shown}，现在这版是 {current_version}）："
        "本次会在**新端口**启动最新版。请把旧的黑窗口关掉，避免两个窗口同时开着。",
    )


def build_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}/"


def build_app(paths: ConsolePaths) -> MediaConsoleApp:
    return MediaConsoleApp(
        rag_root=paths.rag_root,
        media_root=paths.media_root,
        ledger_path=paths.ledger_path,
        env_root=paths.root,
    )


def _open_server(app: MediaConsoleApp, host: str, port: int):
    """创建服务；端口竞争时退回系统分配，保证老板双击一定能起来。"""
    try:
        return create_server(app, host=host, port=port)
    except OSError:
        return create_server(app, host=host, port=0)


def shortcut_powershell_command(*, target: str | Path, name: str = SHORTCUT_NAME) -> str:
    """生成"创建桌面快捷方式"的 PowerShell 命令（纯 ASCII 也可安全传递中文）。"""
    bat = Path(target)
    return (
        "$s=New-Object -ComObject WScript.Shell;"
        "$p=Join-Path ([Environment]::GetFolderPath('Desktop')) "
        f"'{name}.lnk';"
        "$l=$s.CreateShortcut($p);"
        f"$l.TargetPath='{bat}';"
        f"$l.WorkingDirectory='{bat.parent}';"
        "$l.IconLocation='%SystemRoot%\\system32\\shell32.dll,21';"
        "$l.Save();"
        "Write-Output $p"
    )


def create_desktop_shortcut(
    target: str | Path | None = None,
    *,
    name: str = SHORTCUT_NAME,
    runner=subprocess.run,
) -> str:
    """在桌面创建启动快捷方式，返回 .lnk 路径（失败返回空字符串）。"""
    bat = Path(target) if target else project_root() / LAUNCHER_BAT_NAME
    command = shortcut_powershell_command(target=bat, name=name)
    completed = runner(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (getattr(completed, "stdout", "") or "").strip()


def make_shortcut() -> int:
    """CLI 入口：创建桌面快捷方式并给出中文提示。"""
    bat = project_root() / LAUNCHER_BAT_NAME
    print(RULE)
    print("  创建「Pulse素材库」桌面图标")
    print(RULE)
    if not bat.is_file():
        print("创建失败：没有找到启动文件。")
        print(f"  期望位置：{bat}")
        return 2
    try:
        link = create_desktop_shortcut(bat)
    except OSError as exc:  # pragma: no cover - 环境异常
        print(f"创建失败：{exc}")
        return 1
    if link:
        print("创建成功，桌面图标位置：")
        print(f"  {link}")
        print("以后直接双击桌面上的「Pulse素材库」即可打开素材库。")
        return 0
    print("创建失败：系统未返回图标路径，请改用「一键启动_素材库控制台.bat」。")
    return 1


def check_vision(root: str | Path | None = None) -> int:
    """自检视觉模型：接口地址 / 密钥 / 模型是否能识图。"""
    from pulse.services.media.describe import probe_vision, vision_config_from_env

    base = Path(root) if root else project_root()
    config = vision_config_from_env(base)
    print(RULE)
    print("  视觉模型自检")
    print(RULE)
    print(f"  接口地址：{config.endpoint}")
    print(f"  模型：{config.model}")
    print(f"  会话标识：{config.session}（OpenCode Go 必需，缺了会返回 400）")
    key_state = f"已配置（{len(config.api_key)} 位）" if config.enabled else "未配置"
    print(f"  API Key：{key_state}")
    result = probe_vision(config)
    if result.get("ok"):
        print("  结果：可用")
        print(f"  示例描述：{result.get('summary', '')}")
        for warning in result.get("warnings") or []:
            print(f"  提示：{warning}")
        print("  可以正常使用：回到控制台上传图片即可自动生成描述。")
        return 0
    print(f"  结果：不可用（阶段：{result.get('stage')}）")
    print(f"  原因：{result.get('message')}")
    if result.get("hint"):
        print(f"  建议：{result['hint']}")
    if result.get("stage") == "config":
        print("  请在 .env 的 PULSE_VISION_API_KEY 后面填入 Key（等号后不要留空格）。")
    elif result.get("stage") == "model":
        print("  请把 .env 里的 PULSE_VISION_MODEL 改成推荐模型。")
    else:
        print("  请核对 .env 里的四项配置：")
        print("    PULSE_VISION_BASE_URL / PULSE_VISION_API_KEY")
        print("    PULSE_VISION_MODEL / PULSE_VISION_API_STYLE")
    return 1


def run(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    root: str | Path | None = None,
) -> int:
    """启动控制台；返回进程退出码（0 正常，2 目录缺失）。"""
    paths = resolve_paths(root)
    print(RULE)
    print("  Pulse 素材库控制台")
    print(f"  品牌：{BRAND_NAME}")
    print(f"  版本：{PULSE_VERSION}")
    print(RULE)
    if not paths.ready:
        print("启动失败：没有找到下面这些文件夹——")
        for item in paths.missing:
            print(f"  · {item}")
        print("请确认拷贝的是完整文件夹（应包含 RAG知识库 及其上一层的 菲美得产品图片）。")
        return 2

    reuse, start_port, message = plan_start(host, port)
    if message:
        print(message)
    if reuse:
        url = build_url(host, start_port)
        print(f"  访问地址：{url}")
        print(RULE)
        if open_browser:
            _open_browser(url)
        return 0

    app = build_app(paths)
    server = _open_server(app, host, start_port)
    actual_port = int(server.server_address[1])
    url = build_url(host, actual_port)
    capacity = app.state()["capacity"]
    print("启动成功。")
    print(f"  访问地址：{url}")
    print(f"  可召回图片：{capacity['available']} 张（预警阈值 {capacity['threshold']} 张）")
    print(
        "  入库许可：必须先完成视觉识别"
        if app.require_vision
        else "  入库许可：已关闭（应急模式，未经识别的图也能入库）"
    )
    if capacity["alert"] == "red":
        print("  注意：可用图片已低于阈值，页面顶部会显示红色预警，请尽快补拍素材。")
    print("  浏览器会自动打开；用完后关闭本窗口即可退出。")
    print(RULE)

    if open_browser:
        _open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - 手工关闭窗口
        print("\n已退出素材库控制台。")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 手工入口
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Pulse 素材库控制台一键启动")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", default=None, help="仓库根目录（默认自动定位）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--create-shortcut", action="store_true", help="只创建桌面快捷方式")
    parser.add_argument("--check-vision", action="store_true", help="自检视觉模型配置")
    args = parser.parse_args(argv)
    if args.create_shortcut:
        return make_shortcut()
    if args.check_vision:
        return check_vision(args.root)
    return run(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        root=args.root,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
