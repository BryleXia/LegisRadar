"""七、看板出图。

行级渲染（机关、位阶、日期、参与度计数、手动标记）在 test_browser.py 里
用真浏览器验；这里验的是出图这一侧：结构、内联数据的转义、以及数据块里
该有的东西都有。
"""

from __future__ import annotations

import datetime as _dt
import json
import re

import pytest

from conftest import index_blob, item, run_cli, write_config, write_index
from legisradar import __version__, board, config, models, store

SCRIPT_DATA = re.compile(
    r'<script type="application/json" id="lr-data">(.*?)</script>', re.S
)


def make_cfg(root):
    return config.load(root)


def render(root, records, runs=(), today=None, local=False):
    return board.render(
        [models.item_from_record(record, local=local) for record in records],
        list(runs),
        make_cfg(root),
        today=today,
    )


def data_of(html: str) -> dict:
    match = SCRIPT_DATA.search(html)
    assert match is not None
    return json.loads(match.group(1))


def raw_data_of(html: str) -> str:
    match = SCRIPT_DATA.search(html)
    assert match is not None
    return match.group(1)


def count_scripts(html: str) -> int:
    return len(re.findall(r"<script\b", html, re.I))


def group_of(payload: dict, key: str) -> dict:
    return [group for group in payload["groups"] if group["key"] == key][0]


def all_rows(payload: dict) -> list:
    return [row for group in payload["groups"] for row in group["rows"]]


def test_board_is_a_complete_html_document(root):
    html = render(root, [item(title="完整的看板")])
    assert html.startswith("<!doctype html")
    assert html.rstrip().endswith("</html>")
    assert "完整的看板" in html
    assert board.DATA_PLACEHOLDER not in html


def test_embedded_data_round_trips(root):
    titles = ["中华人民共和国甲法（草案）", "乙条例"]
    html = render(root, [item("a:1", titles[0]), item("b:2", titles[1])])
    payload = data_of(html)
    seen = [row["title"] for group in payload["groups"] for row in group["rows"]]
    assert sorted(seen) == sorted(titles)


@pytest.mark.parametrize(
    "probe",
    [
        "</script><img src=x onerror=alert(1)>",
        "<!--<script>",
        "<!--<script>alert(1)</script>",
        "</script>",
    ],
)
def test_hostile_titles_cannot_change_the_script_tag_count(root, probe):
    baseline = count_scripts(board.read_template())
    html = render(root, [item(title=f"草案{probe}标题")])
    assert count_scripts(html) == baseline
    raw = raw_data_of(html)
    assert "<" not in raw
    assert probe not in raw
    assert all_rows(data_of(html))[0]["title"] == f"草案{probe}标题"


def test_closing_script_tag_never_survives_unescaped(root):
    html = render(root, [item(title="草案</script>标题")])
    assert "<img" not in html
    assert "</script>标题" not in html


def test_missing_placeholder_is_an_error(root, monkeypatch):
    monkeypatch.setattr(board, "read_template", lambda: "<html><body>只有外壳</body></html>")
    with pytest.raises(board.BoardError):
        render(root, [item()])


def test_board_data_carries_the_program_version(root):
    html = render(root, [item()])
    payload = data_of(html)
    assert payload["version"] == __version__
    assert payload["about"]["version"] == __version__


def test_items_without_optional_fields_still_render(root):
    html = render(root, [item()])
    payload = data_of(html)
    row = all_rows(payload)[0]
    assert row["tags"] == []
    assert row["links"] == []
    assert row["comments"] is None
    assert row["days"] is None
    assert row["urg"] == 0
    assert row["meta"] == []


def test_no_control_is_left_without_behaviour(root):
    """页面上出现的每个按钮都要有对应行为。

    静态结构这一侧：模板里的按钮就是主题、关于、关闭三枚，都在脚本里接上了
    处理器；条目行上的按钮由脚本现场建，并当场 addEventListener。
    真浏览器里逐枚点过一遍的验证在 test_browser.py。
    """
    template = board.read_template()
    ids = re.findall(r'<button[^>]*id="([^"]+)"', template)
    assert set(ids) == {"theme-toggle", "about-open", "about-close"}
    for ident in ids:
        assert f"'{ident}'" in template
    assert "addEventListener" in template


def test_theme_choice_is_remembered(root):
    html = render(root, [item()])
    assert "legisradar.theme.v1" in html
    assert "localStorage" in html


def test_about_button_and_panel_exist(root):
    html = render(root, [item()])
    assert 'id="about-open"' in html
    assert "<dialog" in html
    assert 'id="about-body"' in html


def test_row_meta_carries_issuer_level_and_dates(root):
    html = render(
        root,
        [
            item(
                issuer="全国人民代表大会常务委员会",
                level="法律",
                published="2026-10-01",
                deadline="2026-10-31",
            )
        ],
        today=_dt.date(2026, 10, 10),
    )
    meta = all_rows(data_of(html))[0]["meta"]
    assert meta[0] == {"k": "text", "v": "全国人民代表大会常务委员会"}
    assert meta[1] == {"k": "lvl", "v": "法律"}
    assert meta[2] == {"k": "date", "v": "2026-10-01 → 2026-10-31"}


@pytest.mark.parametrize(
    "published,deadline,expected",
    [
        ("2026-10-01", "2026-10-31", "2026-10-01 → 2026-10-31"),
        ("", "2026-10-31", "截止 2026-10-31"),
        ("2026-10-01", "", "2026-10-01"),
        ("", "", None),
    ],
)
def test_date_phrase_variants(root, published, deadline, expected):
    html = render(root, [item(published=published, deadline=deadline)])
    meta = all_rows(data_of(html))[0]["meta"]
    dates = [part["v"] for part in meta if part["k"] == "date"]
    assert (dates[0] if dates else None) == expected


def test_comment_count_reaches_the_board(root):
    html = render(root, [item(stats={"comments": 37})])
    row = all_rows(data_of(html))[0]
    assert row["comments"] == 37
    assert "{n}" in data_of(html)["text"]["comments"]


def test_manual_closed_is_marked_on_the_row(root):
    record = dict(item())
    record.update({"user_closed": True, "user_closed_at": "2026-10-01T00:00:00"})
    stored = models.item_from_record(record, local=True)
    html = board.render([stored], [], make_cfg(root))
    row = group_of(data_of(html), "closed")["rows"][0]
    assert row["manual"] is True
    assert data_of(html)["text"]["manual_mark"]


def test_levels_are_the_single_source_of_truth(root):
    html = render(root, [item(level="法律")])
    assert data_of(html)["levels"] == list(models.LEVELS)


def test_level_chips_follow_the_canonical_order(root):
    records = [
        item("a:1", "戊", level="立法计划"),
        item("a:2", "丁", level="部门规章"),
        item("a:3", "丙", level="司法解释"),
        item("a:4", "乙", level="行政法规"),
        item("a:5", "甲", level="法律"),
    ]
    chips = data_of(render(root, records))["level_chips"]
    assert chips == ["全部层级", "法律", "行政法规", "司法解释", "部门规章", "立法计划"]


def test_tag_chips_sort_by_count_with_other_last(root):
    records = [
        item("a:1", "甲", tags=["金融", "其他"]),
        item("a:2", "乙", tags=["金融"]),
        item("a:3", "丙", tags=["金融", "环保"]),
        item("a:4", "丁", tags=["其他"]),
    ]
    tags = [entry["name"] for entry in data_of(render(root, records))["tags"]]
    assert tags[0] == "金融"
    assert tags[-1] == "其他"


def test_empty_groups_are_not_drawn(root):
    html = render(root, [item(deadline="2026-12-31")], today=_dt.date(2026, 10, 10))
    payload = data_of(html)
    soon = [g for g in payload["groups"] if g["key"] == "soon"][0]
    closed = [g for g in payload["groups"] if g["key"] == "closed"][0]
    assert soon["rows"] == []
    assert closed["rows"] == []


def test_summary_counts_cover_the_whole_library(root):
    records = [
        item("a:1", "三天后截止", deadline="2026-10-13"),
        item("a:2", "明年底截止", deadline="2027-12-31"),
        item("a:3", "已经截止了", deadline="2026-01-01"),
    ]
    payload = data_of(render(root, records, today=_dt.date(2026, 10, 10)))
    assert payload["summary"] == {"open": 2, "soon": 1, "closed": 1}


def test_no_deadline_rows_sort_last_and_keep_placeholder(root):
    records = [
        item("a:1", "没有日期"),
        item("a:2", "还有三天", deadline="2026-10-13"),
        item("a:3", "还有二十天", deadline="2026-10-30"),
    ]
    payload = data_of(render(root, records, today=_dt.date(2026, 10, 10)))
    rows = group_of(payload, "open")["rows"]
    assert [row["title"] for row in rows] == ["还有二十天", "没有日期"]
    assert rows[-1]["days"] is None


def test_closed_rows_sort_by_most_recent_deadline(root):
    records = [
        item("a:1", "很久以前", deadline="2020-01-01"),
        item("a:2", "刚过", deadline="2026-10-09"),
        dict(item("a:3", "没有日期"), user_closed=True),
    ]
    payload = data_of(render(root, records, today=_dt.date(2026, 10, 10), local=True))
    rows = group_of(payload, "closed")["rows"]
    assert [row["title"] for row in rows] == ["刚过", "很久以前", "没有日期"]


def test_heartbeat_is_none_without_any_run(root):
    payload = data_of(render(root, [item()]))
    assert payload["heartbeat"]["level"] == ""
    assert payload["heartbeat"]["text"] == board.STRINGS["heartbeat_none"]
    assert payload["library"]["has_run"] is False


@pytest.mark.parametrize(
    "age,level", [(0, "ok"), (7, "ok"), (8, "warn"), (29, "warn"), (30, "bad")]
)
def test_heartbeat_levels_track_data_age(root, age, level):
    today = _dt.date(2026, 10, 10)
    run = {
        "at": (today - _dt.timedelta(days=age)).isoformat() + "T08:00:00",
        "index_generated_at": "2026-10-01T00:00:00",
        "source": "x",
    }
    payload = data_of(render(root, [item()], runs=[run], today=today))
    assert payload["heartbeat"]["level"] == level
    assert payload["heartbeat"]["text"].startswith(board.STRINGS["heartbeat_ok"])


def test_missing_index_timestamp_is_not_faked(root):
    run = {"at": "2026-10-10T08:00:00", "index_generated_at": "", "source": "x"}
    payload = data_of(render(root, [item()], runs=[run], today=_dt.date(2026, 10, 10)))
    assert payload["about"]["index_at"] == board.TEXT["index_missing"]
    assert payload["about"]["index_at"] != payload["generated_at"]


def test_about_panel_lists_only_this_programs_two_files(root):
    cfg = make_cfg(root)
    html = board.render([models.item_from_record(item())], [], cfg)
    about = data_of(html)["about"]
    assert about["items_path"] == "data/items.jsonl"
    assert about["runs_path"] == "data/runs.jsonl"
    assert about["items_path"] != about["runs_path"]
    assert about["data_dir"] == "data"


def test_about_paths_carry_nothing_from_this_machine(root):
    """看板会被整份转发给旁人：配置里写的是相对路径，页面里就得是相对路径。

    这条防的是回归 —— 谁把解析后的绝对路径写进面板，
    程序根（也就是用户名）就跟着页面出去。
    """
    html = render(root, [item()])
    about = data_of(html)["about"]
    assert about["data_dir"] == "data"
    assert about["items_path"] == "data/items.jsonl"
    assert str(root) not in html
    # 盘符路径（C:\… 或 C:/…）。前面挡一个字母，是为了不把 file:// 这类
    # URL 的 scheme 认成盘符。
    assert not re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/]", html), "页面里漏出了盘符路径"
    assert "/home/" not in html
    assert "\\Users\\" not in html


def test_about_shows_an_absolute_dir_the_user_wrote(tmp_path):
    """配置里自己写的绝对路径照原样显示 —— 那是用户的选择，该给他看。"""
    write_config(tmp_path, "[general]\ndata_dir = 'D:\\legisradar-data'\n")
    cfg = config.load(tmp_path)
    about = data_of(board.render([], [], cfg))["about"]
    assert about["data_dir"] == "D:\\legisradar-data"
    assert about["items_path"] == "D:\\legisradar-data\\items.jsonl"


def test_write_board_returns_the_configured_path(root):
    path = write_index(root / "in.json", [item(title="写出去的看板")])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    cfg = make_cfg(root)
    assert cfg.board.read_text(encoding="utf-8").count("写出去的看板") >= 1


def test_board_has_no_external_links(root):
    html = render(root, [item(source_urls=["https://example.gov.cn/a"])])
    head = html.split("</head>", 1)[0]
    assert "http://" not in head.replace("http://www.w3.org", "")
    # 页面里允许出现的 <link> 只有那两枚内联 favicon：没有一条指向外部。
    for tag in re.findall(r"<link\b[^>]*>", html):
        assert 'href="data:' in tag, tag
    assert "src=" not in html.split('<script type="application/json"', 1)[1]


# ------------------------------------------------------------ 图标与配色

ICON = re.compile(r'<link\b[^>]*rel="icon"[^>]*>', re.I)
STYLE = re.compile(r"<style>(.*?)</style>", re.S)
LIGHT_ROOT = re.compile(r"(?m)^:root \{(.*?)^\}", re.S)
DARK_ATTR = re.compile(r'(?m)^:root\[data-theme="dark"\] \{(.*?)^\}', re.S)
STALE_COLORS = ("#151C19", "#3D4A44", "#66756E", "#E9EEEB", "#BFCBC5", "#87978F")


def declared(block: str, prop: str) -> str:
    match = re.search(re.escape(prop) + r"\s*:\s*([^;]+);", block)
    assert match is not None, f"这段声明里没有 {prop}"
    return match.group(1).strip()


def style_of(html: str) -> str:
    match = STYLE.search(html)
    assert match is not None
    return match.group(1)


def test_favicon_is_an_inline_svg_with_a_dark_variant(root):
    head = render(root, [item()]).split("</head>", 1)[0]
    icons = ICON.findall(head)
    assert len(icons) == 2
    assert all('href="data:image/svg+xml' in tag for tag in icons)
    plain = [tag for tag in icons if "media=" not in tag]
    dark = [tag for tag in icons if 'media="(prefers-color-scheme: dark)"' in tag]
    assert len(plain) == 1
    assert len(dark) == 1
    # 浅色那枚用 --accent 的深绿，暗色那枚用暗色主题的浅绿，两边都不发灰。
    assert "%232F5D50" in plain[0]
    assert "%237FB69F" in dark[0]
    # 不带 media 的那枚当兜底，排在前头。
    assert head.index(plain[0]) < head.index(dark[0])


def test_favicon_is_the_header_mark_not_a_flat_disc(root):
    """★ 图标要**照着页眉那个标记画**：一个实心点 + 一圈光晕。

    做成一个铺满画布的圆会变成一块**没有身份的色块** —— 在标签栏上
    只是一个绿点，和页眉那个一眼认得出的标记对不上。

    光晕用**半透明的强调色**而不是固定的浅绿：标签栏什么颜色由浏览器定，
    固定的浅色在白色栏上会消失、在深色栏上会发脏。半透明叠在什么上面都成立。
    """
    import re as _re
    import urllib.parse

    head = render(root, [item()]).split("</head>", 1)[0]
    for tag in ICON.findall(head):
        svg = urllib.parse.unquote(_re.search(r"href=\"data:image/svg\+xml,([^\"]+)\"", tag).group(1))
        circles = _re.findall(r"<circle\b[^>]*>", svg)
        assert len(circles) == 2, f"应该是「点 + 光晕」两个圆，实际 {len(circles)} 个：{svg}"
        outer, inner = circles
        assert "fill-opacity" in outer, "外圈是光晕，必须是半透明的"
        assert "fill-opacity" not in inner, "内圈是那个实心点"
        r = lambda c: float(_re.search(r"r='([\d.]+)'", c).group(1))
        assert r(outer) > r(inner), "光晕要在点的外面"
        # 不能铺满画布 —— 边到边就又是一块色块了
        assert r(outer) < 8, f"光晕半径 {r(outer)} 铺满了 16 的画布，又是一块色块"


def test_text_colors_are_pure_in_light_and_high_in_dark(root):
    css = style_of(render(root, [item()]))
    light = LIGHT_ROOT.search(css)
    attr = DARK_ATTR.search(css)
    assert light is not None
    assert attr is not None
    assert declared(light.group(1), "--ink") == "#000000"
    assert declared(light.group(1), "--ink-2") == "#1A1A1A"
    assert declared(light.group(1), "--muted") == "#4A544F"
    assert declared(attr.group(1), "--ink") == "#FFFFFF"
    assert declared(attr.group(1), "--ink-2") == "#E8E8E8"
    assert declared(attr.group(1), "--muted") == "#A8B5AE"


def test_page_does_not_follow_the_system_theme(root):
    """主题只有两个：浅色、深色。没有「跟随系统」。

    这条钉的是**样式表里不许再有媒体查询** —— 只要还留着一份
    `@media (prefers-color-scheme: dark)`，没打开脚本的读者（或脚本出错时）
    就会落进第三种状态，和按钮上写的两档对不上。
    """
    css = style_of(render(root, [item()]))
    assert "prefers-color-scheme" not in css


def test_theme_starts_light_and_only_two_states_exist(root):
    html = render(root, [item()])
    early = html.split("</head>", 1)[0].split("<script>", 1)[1]
    assert "setAttribute('data-theme', mode)" in early and "var mode = 'light'" in early
    assert "'跟随系统'" not in html
    assert "THEME_MODES = ['light', 'dark']" in html


def test_no_stale_low_contrast_color_survives(root):
    html = render(root, [item()])
    for stale in STALE_COLORS:
        assert stale not in html


def test_button_wording_says_what_the_click_does(root):
    text = data_of(render(root, [item()]))["text"]
    assert text["mark_closed"] == "不再关注"
    assert text["mark_reopen"] == "恢复关注"
    assert text["manual_mark"] == "不再关注"
    assert text["confirm_dismiss"] == "确定把这条移入「已截止」栏吗？之后还可以恢复。"
    # 栏目名不动。
    assert text["group_closed"] == "已截止"


# ------------------------------------------------------------ 相关条目


def test_related_ids_reach_every_row(root):
    records = [item("a:1", "甲", related=["b:2", "c:3"]), item("a:2", "乙")]
    rows = {row["id"]: row for row in all_rows(data_of(render(root, records)))}
    assert rows["a:1"]["related"] == ["b:2", "c:3"]
    assert rows["a:2"]["related"] == []


def test_related_that_is_not_a_list_of_ids_becomes_empty(root):
    records = [
        item("a:1", "不是列表", related="b:2"),
        item("a:2", "列表里有空串", related=["b:1", "", "b:1"]),
    ]
    rows = {row["id"]: row for row in all_rows(data_of(render(root, records)))}
    assert rows["a:1"]["related"] == []
    assert rows["a:2"]["related"] == ["b:1"]
