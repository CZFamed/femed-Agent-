"""品类目录测试：目录扫描、关键词兜底匹配、与模型判断的优先级。"""

from __future__ import annotations

from pulse.services.media.categories import (
    Category,
    CategoryCatalog,
    load_categories,
    match_category,
    resolve_category,
)


def _make_tree(tmp_path):
    root = tmp_path / "图片描述"
    layout = {
        "铸件/阀体": 3,
        "铸件/箱体_支座": 5,
        "加工件/阀体": 1,
        "加工件/机床件": 2,
        "厂区_场景/厂房": 4,
        "人员": 6,
    }
    for folder, count in layout.items():
        directory = root / folder
        directory.mkdir(parents=True)
        for index in range(count):
            (directory / f"a{index}.md").write_text("---\n---\n", encoding="utf-8")
        # 汇总索引不计入条数
        (directory / "00_汇总索引.md").write_text("index", encoding="utf-8")
    return root


def test_load_categories_scans_directories(tmp_path) -> None:
    catalog = load_categories(_make_tree(tmp_path))
    labels = {entry.label for entry in catalog.entries}
    assert "铸件/阀体" in labels
    assert "加工件/机床件" in labels
    assert "人员" in labels, "无子类的品类也要能被选到"
    assert catalog.find("铸件", "阀体").count == 3
    assert catalog.find("人员", "").count == 6
    # 品类按库大小降序：铸件 8 > 人员 6 > 厂区_场景 4 > 加工件 3
    assert catalog.processes == ("铸件", "人员", "厂区_场景", "加工件")


def test_load_categories_on_missing_root_is_empty(tmp_path) -> None:
    catalog = load_categories(tmp_path / "不存在")
    assert catalog.entries == ()
    assert not catalog


def test_catalog_has_and_payload(tmp_path) -> None:
    catalog = load_categories(_make_tree(tmp_path))
    assert catalog.has("铸件", "阀体") is True
    assert catalog.has("铸件", "阀体 ") is True, "前后空格应当被容忍"
    assert catalog.has("铸件", "不存在") is False
    payload = catalog.as_payload()
    assert [item["count"] for item in payload] == sorted(
        (item["count"] for item in payload), reverse=True
    ), "按库大小降序，大品类在前"
    assert payload[0]["label"] == "人员"
    assert all({"process", "sub_process", "label", "count"} <= set(item) for item in payload)


def test_prompt_text_lists_categories(tmp_path) -> None:
    text = load_categories(_make_tree(tmp_path)).prompt_text()
    assert "铸件/阀体" in text
    assert "人员" in text


def test_match_category_uses_keywords(tmp_path) -> None:
    catalog = load_categories(_make_tree(tmp_path))
    assert match_category("画面为堆放整齐的箱体铸件", ("箱体",), catalog) == (
        "铸件",
        "箱体_支座",
        "keyword",
    )
    assert match_category("车间厂房外景", ("厂房",), catalog)[0] == "厂区_场景"


def test_match_category_prefers_bigger_library_on_tie(tmp_path) -> None:
    """只提到"阀体"时，应优先落到库更大的 铸件/阀体，而不是 加工件/阀体。"""
    catalog = load_categories(_make_tree(tmp_path))
    assert match_category("多件阀体铸件堆放", (), catalog) == ("铸件", "阀体", "keyword")


def test_match_category_returns_none_when_nothing_matches(tmp_path) -> None:
    catalog = load_categories(_make_tree(tmp_path))
    assert match_category("一只猫在键盘上", ("猫",), catalog) == ("", "", "none")


def test_resolve_category_prefers_model_answer(tmp_path) -> None:
    catalog = load_categories(_make_tree(tmp_path))
    assert resolve_category("铸件", "箱体_支座", text="", catalog=catalog) == (
        "铸件",
        "箱体_支座",
        "vision",
    )


def test_resolve_category_falls_back_to_keywords(tmp_path) -> None:
    catalog = load_categories(_make_tree(tmp_path))
    assert resolve_category("瞎编的品类", "更瞎编", text="机床工件加工中", catalog=catalog) == (
        "加工件",
        "机床件",
        "keyword",
    )


def test_resolve_category_without_catalog_trusts_model() -> None:
    assert resolve_category("任意品类", "任意子类", catalog=CategoryCatalog()) == (
        "任意品类",
        "任意子类",
        "vision",
    )
    assert resolve_category("", "", catalog=CategoryCatalog()) == ("", "", "none")


def test_category_label_for_singleton() -> None:
    assert Category("人员").label == "人员"
    assert Category("铸件", "阀体").label == "铸件/阀体"
