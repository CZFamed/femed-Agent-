"""一键启动器测试：路径解析、端口挑选、地址构造、缺失目录提示。"""

from __future__ import annotations

import socket

from pulse.console.launcher import (
    ConsolePaths,
    build_app,
    build_url,
    create_desktop_shortcut,
    pick_port,
    resolve_paths,
    run,
    shortcut_powershell_command,
)
from pulse.services.media.config import BRAND_NAME


def _prepare_root(tmp_path):
    """复刻真实布局：仓库根在 <tmp>/agent，实拍素材在它的上一层。"""
    root = tmp_path / "agent"
    (root / "RAG知识库" / "图片描述").mkdir(parents=True)
    (tmp_path / "菲美得产品图片").mkdir(parents=True)
    return root


def test_resolve_paths_ready_when_folders_exist(tmp_path) -> None:
    root = _prepare_root(tmp_path)
    paths = resolve_paths(root)
    assert isinstance(paths, ConsolePaths)
    assert paths.ready is True
    assert paths.missing == ()
    assert paths.rag_root == root / "RAG知识库" / "图片描述"
    assert paths.ledger_path == root / ".pulse" / "media_ledger.sqlite3"


def test_resolve_paths_reports_missing_folders(tmp_path) -> None:
    (tmp_path / "agent").mkdir()
    paths = resolve_paths(tmp_path / "agent")
    assert paths.ready is False
    assert len(paths.missing) == 2
    assert any("RAG" in item for item in paths.missing)
    assert any("菲美得产品图片" in item for item in paths.missing)


def test_resolve_paths_accepts_overrides(tmp_path) -> None:
    root = _prepare_root(tmp_path)
    custom = tmp_path / "其他素材"
    custom.mkdir()
    paths = resolve_paths(
        root, rag_root=custom, media_root=custom, ledger_path=tmp_path / "led.db"
    )
    assert paths.ready is True
    assert paths.rag_root == custom
    assert paths.ledger_path == tmp_path / "led.db"


def test_pick_port_prefers_free_port() -> None:
    assert pick_port(0) > 0


def test_pick_port_falls_back_when_occupied() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        occupied = busy.getsockname()[1]
        chosen = pick_port(occupied)
        assert chosen != occupied
        assert chosen > 0


def test_build_url() -> None:
    assert build_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/"


def test_build_app_uses_brand_pool(tmp_path) -> None:
    root = _prepare_root(tmp_path)
    app = build_app(resolve_paths(root))
    assert app.config.brand == BRAND_NAME == "沧州菲美得"
    assert app.state()["capacity"]["threshold"] == 112


def test_run_returns_2_without_starting_server_when_folders_missing(tmp_path, capsys) -> None:
    code = run(root=tmp_path, open_browser=False)
    assert code == 2
    printed = capsys.readouterr().out
    assert "启动失败" in printed
    assert "RAG" in printed


def test_shortcut_command_contains_target_and_name(tmp_path) -> None:
    bat = tmp_path / "一键启动_素材库控制台.bat"
    command = shortcut_powershell_command(target=bat, name="Pulse素材库")
    assert "WScript.Shell" in command
    assert str(bat) in command
    assert "Pulse素材库.lnk" in command
    assert "GetFolderPath('Desktop')" in command


def test_create_desktop_shortcut_uses_runner(tmp_path) -> None:
    calls: list[list[str]] = []
    options: list[dict] = []

    class FakeCompleted:
        stdout = "C:/Users/boss/Desktop/Pulse素材库.lnk\n"

    def fake_run(args, **kwargs):
        calls.append(args)
        options.append(kwargs)
        return FakeCompleted()

    link = create_desktop_shortcut(tmp_path / "一键启动_素材库控制台.bat", runner=fake_run)
    assert link == "C:/Users/boss/Desktop/Pulse素材库.lnk"
    assert calls and calls[0][0] == "powershell"
    # 中文环境下面板输出必须按 UTF-8 解码，否则 GBK 解码会直接抛异常
    assert options[0].get("encoding") == "utf-8"
    assert options[0].get("errors") == "replace"
