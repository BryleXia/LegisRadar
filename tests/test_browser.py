"""八、看板在真浏览器里。

语法检查通过不等于能跑 —— 这一组每一条都真的把页面丢进无头浏览器渲染一遍，
再从渲染后的 DOM 上取值。页面里插了一段错误捕获脚本：它先于看板脚本挂上
window.onerror，最后把收到的未捕获错误写进一个标记节点。
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
import subprocess
import urllib.request

import pytest

from conftest import dead_url, item, running_server, serve, write_index, run_cli
from legisradar import board, config, models

EDGE = pathlib.Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

pytestmark = pytest.mark.skipif(not EDGE.exists(), reason="这台机器上没有可用的无头浏览器")

PROBE_HEAD = """<script>
window.__lrErrs = [];
window.__lrOut = [];
window.addEventListener('error', function (e) {
  window.__lrErrs.push('error:' + String(e.message || e.type));
});
window.addEventListener('unhandledrejection', function (e) {
  window.__lrErrs.push('rejection:' + String(e.reason));
});
</script>
"""

PROBE_TAIL = """<script>
(function () {
  try { %s } catch (e) { window.__lrErrs.push('action:' + e.message); }
  var box = document.createElement('div');
  box.id = 'lr-probe';
  box.hidden = true;
  box.textContent = 'OUT=' + window.__lrOut.join(' ;; ')
    + ' ERRORS=' + window.__lrErrs.length + ' :: ' + window.__lrErrs.join(' | ');
  document.body.appendChild(box);
})();
</script>
"""


def run_page(tmp_path, html: str, action: str = "") -> str:
    html = html.replace("<head>", "<head>\n" + PROBE_HEAD, 1)
    html = html.replace("</body>", (PROBE_TAIL % action) + "\n</body>", 1)
    page = tmp_path / "page.html"
    page.write_text(html, encoding="utf-8")
    command = [
        str(EDGE),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        f"--user-data-dir={tmp_path / 'profile'}",
        "--no-first-run",
        "--disable-extensions",
        "--virtual-time-budget=6000",
        "--dump-dom",
        page.as_uri(),
    ]
    result = subprocess.run(command, capture_output=True, timeout=180)
    return result.stdout.decode("utf-8", errors="replace")


def probe(dom: str) -> tuple[list[str], list[str]]:
    match = re.search(r'<div id="lr-probe"[^>]*>(.*?)</div>', dom, re.S)
    assert match is not None, "错误捕获脚本没有跑到"
    text = match.group(1)
    out = re.search(r"OUT=(.*?) ERRORS=(\d+) :: ", text, re.S)
    assert out is not None, text
    count = int(out.group(2))
    errors = [] if count == 0 else [part.strip() for part in text.split("::", 1)[1].split("|")]
    values = [] if not out.group(1) else out.group(1).split(" ;; ")
    return values, errors


def visible(dom: str) -> str:
    """去掉脚本、样式与错误捕获脚本，只留真正画出来的那部分。"""
    dom = re.sub(r"<script\b.*?</script>", "", dom, flags=re.S | re.I)
    dom = re.sub(r"<style\b.*?</style>", "", dom, flags=re.S | re.I)
    dom = re.sub(r'<div id="lr-probe"[^>]*>.*?</div>', "", dom, flags=re.S)
    return dom


def rows_in(dom: str) -> int:
    return len(re.findall(r'<li class="row"', dom))


def cfg_for(root):
    return config.load(root)


def board_html(root, records, runs=(), today=None, local=False):
    items = [models.item_from_record(record, local=local) for record in records]
    return board.render(items, list(runs), cfg_for(root), today=today)


# --------------------------------------------------------------- 基本渲染


def test_page_renders_without_uncaught_errors(root, tmp_path):
    dom = run_page(tmp_path, board_html(root, [item(), item("a:2", "第二份草案")]))
    _, errors = probe(dom)
    assert errors == []


def test_rows_really_appear(root, tmp_path):
    records = [item("a:1", "甲"), item("a:2", "乙"), item("a:3", "丙")]
    dom = run_page(tmp_path, board_html(root, records))
    assert rows_in(dom) == 3


def test_heartbeat_line_has_text(root, tmp_path):
    run = {"at": "2026-10-10T08:00:00", "index_generated_at": "2026-10-09T00:00:00", "source": "x"}
    dom = run_page(
        tmp_path,
        board_html(root, [item()], runs=[run], today=_dt.date(2026, 10, 10)),
    )
    block = re.search(r'<div class="heartbeat" id="heartbeat">(.*?)</div>', dom, re.S)
    assert block is not None
    assert re.sub(r"<[^>]+>", "", block.group(1)).strip()
    assert "上次同步" in block.group(1)
    assert 'class="dot"' in block.group(1)


@pytest.mark.parametrize(
    "record",
    [
        {"id": "a:1", "title": "有链接有期限", "source_urls": ["https://example.gov.cn/a"], "deadline": "2026-12-01"},
        {"id": "a:2", "title": "无链接无期限"},
        {"id": "a:3", "title": "只有链接", "source_urls": ["https://example.gov.cn/c"]},
        {"id": "a:4", "title": "被手工收口", "user_closed": True},
    ],
)
def test_every_title_is_rendered(root, tmp_path, record):
    dom = run_page(tmp_path, board_html(root, [record], local=True))
    assert record["title"] in visible(dom)
    assert rows_in(dom) == 1


def test_no_undefined_nan_or_null_on_the_page(root, tmp_path):
    """画出来的每一个单元格都要是内容，不能漏出 undefined / NaN / null。

    只看概览条与条目行 —— 「关于」面板里会列出数据目录路径，
    而路径里出现这几个词是路径自己的事，不是漏出来的值。
    """
    records = [
        item("a:1", "有期限", deadline="2026-12-01", stats={"comments": 12}),
        dict(item("a:2", "无期限无链接")),
        dict(item("a:3", "手工收口"), user_closed=True),
    ]
    action = (
        "window.__lrOut.push(document.getElementById('summary').textContent);"
        "var rows = document.querySelectorAll('#groups li.row');"
        "window.__lrOut.push('rows=' + rows.length);"
        "Array.prototype.forEach.call(rows, function (row) { window.__lrOut.push(row.textContent); });"
    )
    dom = run_page(tmp_path, board_html(root, records, local=True), action=action)
    values, errors = probe(dom)
    assert errors == []
    assert values[1] == "rows=3"
    assert len(values) == 5
    blob = " ;; ".join(values)
    for token in ("undefined", "NaN", "null"):
        assert not re.search(rf"(?<![A-Za-z]){token}(?![A-Za-z])", blob), f"{token} 漏到页面上了：{blob}"
    assert "已有 12 条意见" in blob
    assert "不再关注" in blob


# --------------------------------------------------------------- 四种空态


def test_empty_library_says_the_right_thing(root, tmp_path):
    dom = run_page(tmp_path, board_html(root, []))
    markup = visible(dom)
    assert "当前没有在征的法规草案" in markup
    assert "清空搜索框" not in markup
    assert "已经截止的条目仍然列着" not in markup
    assert "还没有同步到数据" in markup


def test_empty_library_after_a_run_says_the_other_thing(root, tmp_path):
    run = {"at": "2026-10-10T08:00:00", "index_generated_at": "2026-10-09T00:00:00", "source": "x"}
    dom = run_page(tmp_path, board_html(root, [], runs=[run]))
    markup = visible(dom)
    assert "当前没有在征的法规草案" in markup
    assert "清空搜索框" not in markup
    assert "人大全年只有约 5~6 个月有征求意见窗口" in markup
    assert "已经截止的条目仍然列着" not in markup


def test_all_closed_library_shows_the_tail(root, tmp_path):
    dom = run_page(
        tmp_path,
        board_html(
            root,
            [item("a:1", "上个月的草案", deadline="2026-09-01")],
            today=_dt.date(2026, 10, 10),
        ),
    )
    markup = visible(dom)
    assert "当前没有在征的法规草案" in markup
    assert "已经截止的条目仍然列着" in markup
    assert rows_in(dom) == 1


def test_filter_miss_says_clear_the_search_box(root, tmp_path):
    records = [item("a:1", "甲法草案"), item("a:2", "乙法草案")]
    dom = run_page(
        tmp_path,
        board_html(root, records),
        action="var s=document.getElementById('search');s.value='zzzz';"
        "s.dispatchEvent(new Event('input',{bubbles:true}));",
    )
    markup = visible(dom)
    assert "没有符合当前筛选的条目" in markup
    assert "试试清空搜索框" in markup
    assert rows_in(dom) == 0


# --------------------------------------------------------------- 筛选条


def test_level_chip_order_is_forced_by_the_program(root, tmp_path):
    records = [
        item("a:1", "戊", level="立法计划"),
        item("a:2", "丁", level="部门规章"),
        item("a:3", "丙", level="司法解释"),
        item("a:4", "乙", level="行政法规"),
        item("a:5", "甲", level="法律"),
    ]
    dom = run_page(tmp_path, board_html(root, records))
    block = re.search(r'<div class="chips" id="chips-level">(.*?)</div>', dom, re.S)
    assert block is not None
    labels = re.findall(r">([^<>]*)</button>", block.group(1))
    assert labels == ["全部层级", "法律", "行政法规", "司法解释", "部门规章", "立法计划"]


def test_level_table_matches_the_model_exactly(root, tmp_path):
    dom = run_page(
        tmp_path,
        board_html(root, [item(level="法律")]),
        action="window.__lrOut.push(JSON.parse("
        "document.getElementById('lr-data').textContent).levels.join(','));",
    )
    values, _ = probe(dom)
    assert values == [",".join(models.LEVELS)]


def test_level_chip_toggles_off_when_clicked_again(root, tmp_path):
    records = [item("a:1", "甲", level="法律"), item("a:2", "乙", level="部门规章")]
    action = (
        "var chips=document.querySelectorAll('#chips-level .chip');"
        "chips[1].click();window.__lrOut.push('rows=' + document.querySelectorAll('li.row').length);"
        "chips[1].click();window.__lrOut.push('rows=' + document.querySelectorAll('li.row').length);"
    )
    dom = run_page(tmp_path, board_html(root, records), action=action)
    values, errors = probe(dom)
    assert errors == []
    assert values == ["rows=1", "rows=2"]


# --------------------------------------------------------------- 链接安全


HOSTILE = [
    "javascript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    "\x00javascript:alert(1)",
    "  javascript:alert(1)",
    "//evil.example/x",
    "\\\\evil.example/x",
    "/\\evil.example/x",
]


def test_hostile_links_never_reach_href(root, tmp_path):
    records = [
        item(f"a:{index}", f"第 {index} 份草案", source_urls=[url])
        for index, url in enumerate(HOSTILE)
    ]
    records.append(item("ok:1", "合法的那一份", source_urls=["https://example.gov.cn/policy"]))
    dom = run_page(tmp_path, board_html(root, records))
    hrefs = re.findall(r'href="([^"]*)"', dom)
    for href in hrefs:
        assert not href.lower().startswith("javascript:")
        assert not href.startswith("//")
    assert "https://example.gov.cn/policy" in hrefs


def test_sanitize_url_blocks_the_eight_shapes():
    for value in HOSTILE:
        assert models.sanitize_url(value) == ""
    assert models.sanitize_url("https://example.gov.cn/a") == "https://example.gov.cn/a"
    assert models.sanitize_url("http://example.gov.cn/a") == "http://example.gov.cn/a"


def test_legit_links_are_still_clickable(root, tmp_path):
    records = [item("a:1", "有链接的草案", source_urls=["https://example.gov.cn/one"])]
    dom = run_page(tmp_path, board_html(root, records))
    # 只有一条链接：标题自己就是那个链接。
    anchors = re.findall(r'<a href="([^"]+)"[^>]*target="_blank"', dom)
    assert anchors == ["https://example.gov.cn/one"]


def test_view_original_button_only_appears_for_several_links(root, tmp_path):
    records = [
        item("a:1", "一条链接的草案", source_urls=["https://example.gov.cn/one"]),
        item(
            "a:2",
            "两条链接的草案",
            source_urls=["https://example.gov.cn/two", "https://example.gov.cn/three"],
        ),
    ]
    dom = run_page(tmp_path, board_html(root, records))
    assert re.findall(r'<a class="link"[^>]*href="([^"]+)"', dom) == [
        "https://example.gov.cn/two",
        "https://example.gov.cn/three",
    ]
    assert visible(dom).count("查看原文") == 2


def test_titles_without_a_link_are_plain_text(root, tmp_path):
    dom = run_page(tmp_path, board_html(root, [item("a:1", "没有链接的草案")]))
    assert rows_in(dom) == 1
    assert "本条目没有可打开的原文链接" in visible(dom)
    assert 'href="#"' not in dom


# --------------------------------------------------------------- 控件行为


def test_group_header_collapses_and_expands(root, tmp_path):
    action = (
        "var head=document.querySelector('.group-hd');"
        "var list=head.parentNode.querySelector('.rows');"
        "window.__lrOut.push('start=' + list.hidden);"
        "head.click();window.__lrOut.push('after=' + list.hidden);"
        "head.click();window.__lrOut.push('back=' + list.hidden);"
    )
    dom = run_page(tmp_path, board_html(root, [item()]), action=action)
    values, errors = probe(dom)
    assert errors == []
    assert values == ["start=false", "after=true", "back=false"]


def test_theme_button_changes_and_remembers(root, tmp_path):
    action = (
        "var b=document.getElementById('theme-toggle');"
        "b.click();window.__lrOut.push('1=' + (document.documentElement.getAttribute('data-theme')||'NONE'));"
        "b.click();window.__lrOut.push('2=' + (document.documentElement.getAttribute('data-theme')||'NONE'));"
        "window.__lrOut.push('stored=' + window.localStorage.getItem('legisradar.theme.v1'));"
        "b.click();window.__lrOut.push('3=' + (document.documentElement.getAttribute('data-theme')||'NONE'));"
    )
    dom = run_page(tmp_path, board_html(root, [item()]), action=action)
    values, errors = probe(dom)
    assert errors == []
    # 起步是浅色；点一下换深色，再点回浅色。没有第三档。
    assert values == ["1=dark", "2=light", "stored=light", "3=dark"]


def test_about_button_opens_the_panel(root, tmp_path):
    action = (
        "var d=document.getElementById('about');"
        "window.__lrOut.push('before=' + d.open);"
        "document.getElementById('about-open').click();"
        "window.__lrOut.push('after=' + d.open);"
        "document.getElementById('about-close').click();"
        "window.__lrOut.push('closed=' + d.open);"
    )
    dom = run_page(tmp_path, board_html(root, [item()]), action=action)
    values, errors = probe(dom)
    assert errors == []
    assert values == ["before=false", "after=true", "closed=false"]


def test_serve_hint_shows_only_outside_serve_mode(root, tmp_path):
    dom = run_page(tmp_path, board_html(root, [item()]))
    assert "想给条目点「已截止」" in visible(dom)
    assert "已截止" not in re.findall(r'<button class="btn">([^<]*)</button>', dom)


def test_serve_mode_shows_the_button_and_hides_the_hint(root, tmp_path):
    index = write_index(root / "in.json", [item("a:1", "服务模式下的一条")])
    assert run_cli(["sync", "--from", str(index)], root)[0] == 0
    cfg = config.load(root)
    with running_server(cfg, serve.new_token()) as (_, port):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
            served = response.read().decode("utf-8")
    dom = run_page(tmp_path, served)
    markup = visible(dom)
    assert "想给条目点「已截止」" not in markup
    assert "不再关注" in markup
    assert rows_in(dom) == 1


def test_dismiss_asks_first_and_cancel_leaves_the_button_usable(root, tmp_path):
    """点「不再关注」先弹确认框；点「取消」时一个请求都不发，按钮还能再点。"""
    html = board_html(root, [item("a:1", "要点一下的一条")])
    html = html.replace(board.TOKEN_META, serve.token_meta("0" * 32), 1)
    action = (
        "var calls=0;"
        "window.fetch=function(){calls+=1;return new Promise(function(){});};"
        "var asked=0;var answer=false;"
        "window.confirm=function(){asked+=1;return answer;};"
        "var b=document.querySelector('li.row .btn');"
        "window.__lrOut.push('label=' + b.textContent);"
        "b.click();"
        "window.__lrOut.push('cancel: asked=' + asked + ' calls=' + calls + ' disabled=' + b.disabled);"
        "answer=true;"
        "b.click();"
        "window.__lrOut.push('ok: asked=' + asked + ' calls=' + calls + ' disabled=' + b.disabled);"
    )
    dom = run_page(tmp_path, html, action=action)
    values, errors = probe(dom)
    assert errors == []
    assert values == [
        "label=不再关注",
        "cancel: asked=1 calls=0 disabled=false",
        "ok: asked=2 calls=1 disabled=true",
    ]


# --------------------------------------------------------------- 相关条目


def test_related_items_carry_title_link_and_remaining_days(root, tmp_path):
    records = [
        item("a:1", "主条目", related=["a:2", "a:3", "a:4", "gone:9"]),
        item("a:2", "相关的乙", source_urls=["https://example.gov.cn/b"], deadline="2026-10-19"),
        item("a:3", "没有链接的相关条目"),
        item("a:4", "已经截止的相关条目", deadline="2026-01-01"),
    ]
    dom = run_page(tmp_path, board_html(root, records, today=_dt.date(2026, 10, 10)))
    block = re.search(r'<details class="related">(.*?)</details>', dom, re.S)
    assert block is not None
    inner = block.group(1)
    assert "语义相关的还有 4 条" in inner
    assert '<a href="https://example.gov.cn/b"' in inner
    assert ">相关的乙</a>" in inner
    assert "还剩 9 天" in inner
    assert "无期限" in inner
    assert "已截止" in inner
    assert "（这条已不在库中）" in inner
    # 没有可用链接的那条：只列标题，不做成能点的样子。
    assert ">没有链接的相关条目</span>" in inner
    assert 'href="#"' not in dom


def test_related_block_is_absent_when_nothing_relates(root, tmp_path):
    dom = run_page(tmp_path, board_html(root, [item("a:1", "孤零零的一条")]))
    assert rows_in(dom) == 1
    assert "<details" not in dom
    assert "语义相关的还有" not in visible(dom)
