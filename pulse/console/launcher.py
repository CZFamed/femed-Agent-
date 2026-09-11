"""一键启动素材库控制台（面向非技术同事）。

双击仓库根目录的「一键启动_素材库控制台.bat」即会调用本模块：自动定位素材目录、
挑选空闲端口、打开浏览器，并用中文提示当前状态与容量预警。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
    return ConsolePaths(rag_root=rag, media_root=media, ledger_path=ledger, missing=missing)


def pick_port(preferred: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> int:
    """优先用 preferred；被占用时由系统分配一个空闲端口。"""
    with socket.socket() as probe:
        try:
            probe.bind((host, preferred))
            return int(probe.getsockname()[1])
        except OSError:
            pass
    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def build_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}/"


def build_app(paths: ConsolePaths) -> MediaConsoleApp:
    return MediaConsoleApp(
        rag_root=paths.rag_root,
        media_root=paths.media_root,
        ledger_path=paths.ledger_path,
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
    print(RULE)
    if not paths.ready:
        print("启动失败：没有找到下面这些文件夹——")
        for item in paths.missing:
            print(f"  · {item}")
        print("请确认拷贝的是完整文件夹（应包含 RAG知识库 及其上一层的 菲美得产品图片）。")
        return 2

    app = build_app(paths)
    server = _open_server(app, host, port)
    actual_port = int(server.server_address[1])
    url = build_url(host, actual_port)
    capacity = app.state()["capacity"]
    print("启动成功。")
    print(f"  访问地址：{url}")
    print(f"  可召回图片：{capacity['available']} 张（预警阈值 {capacity['threshold']} 张）")
    if capacity["alert"] == "red":
        print("  注意：可用图片已低于阈值，页面顶部会显示红色预警，请尽快补拍素材。")
    print("  浏览器会自动打开；用完后关闭本窗口即可退出。")
    print(RULE)

    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # pragma: no cover - 浏览器不可用不影响服务
            print("浏览器未自动打开，请手动复制上面的地址到浏览器。")
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
    args = parser.parse_args(argv)
    if args.create_shortcut:
        return make_shortcut()
    return run(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        root=args.root,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
