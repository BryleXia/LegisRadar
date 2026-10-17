"""二、索引取用与校验。三、条目进门。"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re

import pytest

from conftest import (
    capture_logs,
    dead_url,
    http_server,
    index_blob,
    item,
    run_cli,
    write_config,
    write_index,
)
from legisradar import models, store, sync

CONFIG_TEMPLATE = """\
[general]
data_dir = "data"
board = "board.html"

[viewer]
index_url = {primary}
index_mirrors = [{mirrors}]

[serve]
port = 8765
"""


def configure(root: pathlib.Path, primary: str, mirrors=()):
    quoted = ", ".join(f'"{m}"' for m in mirrors)
    write_config(root, CONFIG_TEMPLATE.format(primary=f'"{primary}"', mirrors=quoted))
    return root


def library(root: pathlib.Path):
    return store.read_items(root / "data")


def board_data(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="lr-data">(.*?)</script>', html, re.S
    )
    assert match is not None
    return json.loads(match.group(1))


# ------------------------------------------------------------ 信封与 schema


def test_schema_mismatch_rejects_whole_index(root):
    path = root / "in.json"
    path.write_text(index_blob([item()], schema=3), encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 1
    assert "3" in out and "2" in out
    assert not library(root)


def test_missing_items_list_rejects_whole_index(root):
    path = root / "in.json"
    path.write_text(json.dumps({"schema": 2, "generated_at": "x"}), encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 1
    assert not library(root)


def test_top_level_not_an_object_rejects(root):
    path = root / "in.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 1


@pytest.mark.parametrize("bad", [1.0, True, "1", None, 1.5])
def test_schema_must_be_a_real_integer(root, bad):
    path = root / "in.json"
    path.write_text(json.dumps({"schema": bad, "items": []}), encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 1
    assert "schema" in out


def test_bad_schema_with_local_data_keeps_library(root):
    good = write_index(root / "good.json", [item(), item("src:bbb", "另一部草案")])
    assert run_cli(["sync", "--from", str(good)], root)[0] == 0
    before = (root / "data" / "items.jsonl").read_bytes()

    bad = root / "bad.json"
    bad.write_text(index_blob([item()], schema=9), encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(bad)], root)
    assert code == 0
    assert "这次没同步上" in out
    assert (root / "data" / "items.jsonl").read_bytes() == before


def test_bad_schema_with_empty_library_fails(root):
    bad = root / "bad.json"
    bad.write_text(index_blob([item()], schema=9), encoding="utf-8")
    assert run_cli(["sync", "--from", str(bad)], root)[0] == 1


def test_local_file_source_reports_the_path_used(root):
    path = write_index(root / "somewhere.json", [item()])
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 0
    assert str(path) in out


def test_local_index_that_is_not_json_fails_loudly(root):
    path = root / "in.json"
    path.write_text("这不是 JSON", encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 1
    assert "JSON" in out


# ------------------------------------------------------------ 地址与镜像


def test_all_addresses_unreachable_lists_every_one(root):
    primary = dead_url()
    mirrors = [dead_url(), dead_url()]
    configure(root, primary, mirrors)
    code, out, err = run_cli(["sync"], root)
    assert code == 1
    assert "这次没同步上" in out
    for address in [primary, *mirrors]:
        assert address in out


def test_offline_with_local_data_still_succeeds(root):
    good = write_index(root / "good.json", [item()])
    assert run_cli(["sync", "--from", str(good)], root)[0] == 0
    before = (root / "data" / "items.jsonl").read_bytes()

    configure(root, dead_url())
    code, out, err = run_cli(["sync"], root)
    assert code == 0
    assert "这次没同步上" in out
    assert (root / "data" / "items.jsonl").read_bytes() == before
    assert (root / "board.html").is_file()


def test_offline_with_empty_library_fails(root):
    configure(root, dead_url())
    code, out, err = run_cli(["sync"], root)
    assert code == 1
    assert "本地一条数据都没有" in out


def test_断网本地空时自动用随包的索引(root):
    """全新解压、第一次打开、偏偏没网 —— 这是最可能发生的第一次。

    随包那份就躺在同一个文件夹里。让程序自己读它，比打印一行命令让
    用户照着敲更可靠：他要面对的是一个刚双击开、可能连命令行都没见过的
    东西。
    """
    write_index(root / "index.json", [item("a:1", "随包那条")],
                generated_at="2026-10-14T09:00:00")
    configure(root, dead_url())
    code, out, err = run_cli(["sync"], root)

    assert code == 0, "有东西可看就不该报失败 —— 退出码说的是能不能看，不是网络成没成"
    assert "随包" in out and "2026-10-14" in out
    assert [i.title for i in library(root)] == ["随包那条"]
    assert "随包那条" in (root / "board.html").read_text(encoding="utf-8")


def test_本地有数据时不吃随包那份(root):
    """本地是新的、随包是旧的。这时候把随包的并进来是倒退，
    而且从界面上完全看不出来 —— 用户只知道「同步之后少了几条」。"""
    good = write_index(root / "good.json", [item("a:live", "本地的")])
    assert run_cli(["sync", "--from", str(good)], root)[0] == 0

    write_index(root / "index.json", [item("b:old", "随包的")])
    configure(root, dead_url())
    assert run_cli(["sync"], root)[0] == 0
    assert [i.id for i in library(root)] == ["a:live"]


def test_随包索引坏了也不崩(root):
    (root / "index.json").write_text("{半截", encoding="utf-8")
    configure(root, dead_url())
    code, out, err = run_cli(["sync"], root)
    assert code == 1
    assert "本地一条数据都没有" in out
    assert "也用不了" in out, "得说清是随包那份坏了，不是别的原因"


def test_随包索引是空的也算没救(root):
    write_index(root / "index.json", [])
    configure(root, dead_url())
    code, out, err = run_cli(["sync"], root)
    assert code == 1
    assert "本地一条数据都没有" in out


def test_bundled_index_hint_is_absent_when_there_is_no_bundled_index(root):
    configure(root, dead_url())
    code, out, err = run_cli(["sync"], root)
    assert code == 1
    assert "--from index.json" not in out


def test_no_index_url_points_at_the_config_key(root):
    configure(root, "")
    code, out, err = run_cli(["sync"], root)
    assert code == 1
    assert "index_url" in out


def test_index_with_rows_but_none_usable(root):
    path = root / "in.json"
    rows = [{"title": "缺 id"}, {"id": "src:1"}, "不是对象但没关系", {"id": "", "title": ""}]
    path.write_text(index_blob(rows), encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 1
    assert "没有一条能用" in out
    assert not (root / "data" / "runs.jsonl").exists()


def test_skipped_rows_are_reported_and_the_rest_kept(root):
    path = write_index(root / "in.json", [item(), {"title": "缺 id"}, {"id": "src:z"}])
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 0
    assert "已跳过" in out
    assert len(library(root)) == 1


def test_run_record_is_written(root):
    path = write_index(root / "in.json", [item()], generated_at="2026-10-08T07:30:00")
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    record = store.last_run(root / "data")
    assert record is not None
    assert record["index_generated_at"] == "2026-10-08T07:30:00"
    assert record["source"] == str(path)
    assert record["at"]


def test_sync_rebuilds_the_board(root):
    path = write_index(root / "in.json", [item(title="独一无二的标题")])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    html = (root / "board.html").read_text(encoding="utf-8")
    assert "独一无二的标题" in html


def test_index_with_utf8_bom_is_readable(root):
    path = root / "in.json"
    path.write_bytes(b"\xef\xbb\xbf" + index_blob([item()]).encode("utf-8"))
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 0
    assert len(library(root)) == 1


def test_oversized_local_index_is_rejected(root, monkeypatch):
    monkeypatch.setattr(sync, "MAX_INDEX_BYTES", 2048)
    path = write_index(root / "in.json", [item()])
    path.write_text(path.read_text(encoding="utf-8") + " " * 4096, encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 1
    assert "上限" in out


def test_oversized_remote_index_is_rejected(root, monkeypatch):
    monkeypatch.setattr(sync, "MAX_INDEX_BYTES", 2048)
    body = index_blob([item()]) + " " * 4096
    with http_server([(200, body)]) as (url, seen):
        code, out, err = run_cli(["sync", "--from", url], root)
    assert code == 1
    assert "上限" in out


def test_size_limit_leaves_room_for_a_real_index():
    assert sync.MAX_INDEX_BYTES >= 16 * 1024 * 1024


def test_from_does_not_try_mirrors(root):
    with http_server([(200, index_blob([item(title="来自镜像")]))]) as (mirror, seen):
        configure(root, dead_url(), [mirror])
        code, out, err = run_cli(["sync", "--from", dead_url()], root)
    assert seen == []
    assert code == 1


def test_default_path_falls_back_to_mirrors(root):
    with http_server([(200, index_blob([item(title="来自镜像")]))]) as (mirror, seen):
        configure(root, dead_url(), [mirror])
        code, out, err = run_cli(["sync"], root)
    assert seen, "镜像没有被访问"
    assert code == 0
    assert mirror in out
    assert len(library(root)) == 1


def test_primary_works_without_touching_mirrors(root):
    with http_server([(200, index_blob([item(title="来自主地址")]))]) as (mirror, seen):
        configure(root, "", [])
        with http_server([(200, index_blob([item(title="来自主地址")]))]) as (primary, seen2):
            configure(root, primary, [mirror])
            code, out, err = run_cli(["sync"], root)
        assert seen == []
    assert code == 0
    assert primary in out


# ------------------------------------------------------------ 条目进门


def test_unknown_fields_are_ignored(root):
    row = item(whatever="多余字段", nested={"a": [1, 2]})
    path = write_index(root / "in.json", [row])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    assert len(library(root)) == 1


def test_related_ids_survive_the_sync(root):
    path = write_index(root / "in.json", [item(related=["b:2", "c:3"])])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    assert library(root)[0].related == ["b:2", "c:3"]


def test_related_ids_refresh_on_the_next_sync(root):
    """跑批重算出来的相近条目要能盖掉上一轮那份，不是只在头一次进库时记一次。"""
    first = write_index(root / "first.json", [item(title="甲法草案", related=["b:1"])])
    assert run_cli(["sync", "--from", str(first)], root)[0] == 0
    second = write_index(root / "second.json", [item(title="甲法草案", related=["c:9"])])
    assert run_cli(["sync", "--from", str(second)], root)[0] == 0
    assert library(root)[0].related == ["c:9"]


def test_missing_id_or_title_row_is_skipped(root):
    rows = [item(), {"title": "没有 id"}, {"id": "src:no-title"}, {"id": "", "title": "空 id"}]
    path = write_index(root / "in.json", rows)
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    assert [entry.id for entry in library(root)] == ["src:aaa"]


def test_non_http_links_are_dropped_from_the_same_field(root):
    row = item(
        source_urls=[
            "javascript:alert(1)",
            "https://example.gov.cn/a",
            "data:text/html;base64,QQ==",
            "vbscript:msgbox",
            "http://example.gov.cn/b",
        ]
    )
    path = write_index(root / "in.json", [row])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    assert library(root)[0].source_urls == [
        "https://example.gov.cn/a",
        "http://example.gov.cn/b",
    ]


def test_http_and_https_links_are_kept_verbatim(root):
    urls = ["http://a.example.cn/x?y=1#z", "https://b.example.cn/"]
    path = write_index(root / "in.json", [item(source_urls=urls)])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    assert library(root)[0].source_urls == urls


@pytest.mark.parametrize(
    "field,bad",
    [
        ("tags", "门类是个字符串"),
        ("tags", {"门类": "是个字典"}),
        ("tags", 123),
        ("source_urls", "https://example.gov.cn"),
        ("stats", "参与度是个字符串"),
        ("user_closed", "yes"),
        ("user_closed", 1),
    ],
)
def test_wrong_field_types_degrade_and_keep_the_row(field, bad):
    parsed = models.item_from_record(item(**{field: bad}))
    assert parsed is not None
    if field in ("tags", "source_urls"):
        assert parsed.tags == [] and parsed.source_urls == []
    elif field == "stats":
        assert parsed.stats == {}
    else:
        assert parsed.user_closed is False


@pytest.mark.parametrize(
    "field,bad,expected",
    [("tags", "x", []), ("tags", {"a": 1}, []), ("stats", "x", {}), ("source_urls", "x", [])],
)
def test_wrong_types_reach_the_board_as_real_containers(root, field, bad, expected):
    path = write_index(root / "in.json", [item(**{field: bad})])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    data = board_data((root / "board.html").read_text(encoding="utf-8"))
    rows = [row for group in data["groups"] for row in group["rows"]]
    assert len(rows) == 1
    key = {"tags": "tags", "stats": "comments", "source_urls": "links"}[field]
    value = rows[0][key]
    assert isinstance(value, list) if key != "comments" else value is None


def test_numeric_deadline_becomes_a_string_literal(root):
    assert models.item_from_record(item(deadline=20261130)).deadline == "20261130"

    path = write_index(root / "in.json", [item(deadline=20261101, published=20261001)])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    stored = library(root)[0]
    assert stored.deadline == "20261101"
    assert models.days_left(stored, _dt.date(2026, 10, 25)) == 7
    assert models.is_open(stored, _dt.date(2026, 10, 25)) is True


def test_numeric_title_becomes_a_string(root):
    parsed = models.item_from_record({"id": "src:1", "title": 20261130})
    assert parsed is not None
    assert parsed.title == "20261130"


def test_non_string_id_drops_the_whole_row():
    assert models.item_from_record({"id": {"a": 1}, "title": "标题"}) is None
    assert models.item_from_record({"id": None, "title": "标题"}) is None


def test_list_fields_on_the_board_are_really_lists(root):
    path = write_index(root / "in.json", [item(tags="不是列表", source_urls="也不是")])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    data = board_data((root / "board.html").read_text(encoding="utf-8"))
    rows = [row for group in data["groups"] for row in group["rows"]]
    assert rows and rows[0]["tags"] == [] and rows[0]["links"] == []


def test_non_object_rows_are_counted_and_warned(root):
    rows = [None] * 190 + [item(f"src:{n}", f"草案 {n}") for n in range(10)]
    path = root / "in.json"
    path.write_text(index_blob(rows), encoding="utf-8")
    with capture_logs() as logs:
        code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 0
    assert "共 10 条" in out
    assert "190" in out
    assert any("190" in message and "不是对象" in message for message in logs.messages)
