"""四、本地库。五、去重与合并。六、并发的写。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import threading
import time

import pytest

from conftest import index_blob, item, run_cli, write_index
from legisradar import board, models, store


def titles(data_dir: pathlib.Path):
    return sorted(entry.title for entry in store.read_items(data_dir))


def by_id(data_dir: pathlib.Path):
    return {entry.id: entry for entry in store.read_items(data_dir)}


# ------------------------------------------------------------------ 基本读写


def test_new_item_records_first_seen_and_counts_as_added(tmp_path):
    data = tmp_path / "data"
    stats = store.merge_items(data, [models.item_from_record(item())])
    assert stats.added == 1
    stored = store.read_items(data)[0]
    assert stored.first_seen
    _dt.datetime.fromisoformat(stored.first_seen)


def test_second_merge_of_same_item_is_not_new(tmp_path):
    data = tmp_path / "data"
    first = store.merge_items(data, [models.item_from_record(item())])
    second = store.merge_items(data, [models.item_from_record(item())])
    assert first.added == 1
    assert second.added == 0
    assert second.unchanged == 1


def test_filled_deadline_survives_a_later_empty_value(tmp_path):
    data = tmp_path / "data"
    store.merge_items(data, [models.item_from_record(item(deadline="2026-11-30"))])
    store.merge_items(data, [models.item_from_record(item())])
    assert store.read_items(data)[0].deadline == "2026-11-30"


def test_source_links_accumulate(tmp_path):
    data = tmp_path / "data"
    store.merge_items(data, [models.item_from_record(item(source_urls=["https://a.cn/1"]))])
    store.merge_items(data, [models.item_from_record(item(source_urls=["https://b.cn/2"]))])
    assert store.read_items(data)[0].source_urls == ["https://a.cn/1", "https://b.cn/2"]


@pytest.mark.parametrize("bad", [b"null", b"42", b"[]", b'"abc"', b"true"])
def test_broken_lines_are_skipped_and_good_ones_kept(tmp_path, bad):
    data = tmp_path / "data"
    data.mkdir()
    good = json.dumps({"id": "src:ok", "title": "好行"})
    (data / "items.jsonl").write_bytes(bad + b"\n" + good.encode("utf-8") + b"\n")
    assert titles(data) == ["好行"]


def test_half_written_json_line_is_skipped(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    good = json.dumps({"id": "src:ok", "title": "好行"})
    half = '{"id": "src:x", "title": "写到一半'.encode("utf-8")
    (data / "items.jsonl").write_bytes(
        good.encode("utf-8") + b"\n" + half + b"\n" + good.encode("utf-8") + b"\n"
    )
    assert titles(data) == ["好行", "好行"]


@pytest.mark.parametrize(
    "record,field",
    [
        ({"id": 123, "title": "数字 id"}, "id"),
        ({"id": "src:1", "title": 456}, "title"),
        ({"id": "src:2", "title": "标题", "tags": "不是数组"}, "tags"),
        ({"id": "src:3", "title": "标题", "deadline": ["2026", "11", "1"]}, "deadline"),
    ],
)
def test_field_level_damage_keeps_the_row(tmp_path, record, field):
    data = tmp_path / "data"
    data.mkdir()
    (data / "items.jsonl").write_bytes(
        json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
    )
    items = store.read_items(data)
    assert len(items) == 1
    value = getattr(items[0], field)
    assert value in ("123", "456", "", [])


def test_row_whose_id_normalises_to_empty_is_skipped(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    rows = [
        {"id": {"a": 1}, "title": "字典 id"},
        {"id": "src:ok", "title": "好行"},
    ]
    (data / "items.jsonl").write_bytes(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8") + b"\n"
    )
    assert titles(data) == ["好行"]


def test_invalid_utf8_bytes_do_not_raise(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    good = json.dumps({"id": "src:ok", "title": "好行"}).encode("utf-8")
    (data / "items.jsonl").write_bytes(b"\xff\xfe not utf-8\n" + good + b"\n")
    assert titles(data) == ["好行"]


def test_board_survives_a_broken_library_line(root):
    data = root / "data"
    data.mkdir()
    (data / "items.jsonl").write_bytes(
        b"\x00\x01 broken\n" + json.dumps({"id": "src:ok", "title": "好行"}).encode("utf-8") + b"\n"
    )
    code, out, err = run_cli(["board"], root)
    assert code == 0
    assert (root / "board.html").is_file()


def test_runs_are_readable_and_the_last_one_wins(tmp_path):
    data = tmp_path / "data"
    store.record_run(data, at="2026-01-01T00:00:00", index_generated_at="2025-12-31T00:00:00", source="a")
    store.record_run(data, at="2026-02-02T00:00:00", index_generated_at="2026-02-01T00:00:00", source="b")
    record = store.last_run(data)
    assert record is not None
    assert record["at"] == "2026-02-02T00:00:00"
    assert record["source"] == "b"


def test_wrong_shaped_run_record_is_treated_as_none(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "runs.jsonl").write_bytes(b"[1,2,3]\n")
    assert store.read_runs(data) == []
    assert store.last_run(data) is None


def test_broken_bytes_in_side_files_never_reach_the_board(root):
    data = root / "data"
    data.mkdir()
    (data / "items.jsonl").write_bytes(b"\xff\xff\xff\n")
    (data / "runs.jsonl").write_bytes(b"\x00\x00\n")
    code, out, err = run_cli(["board"], root)
    assert code == 0


# ------------------------------------------------------------------ 日期


@pytest.mark.parametrize("value", [20261101, ["2026-11-01"], {"d": 1}, True, None, 1.5])
def test_date_layer_only_accepts_strings(value):
    assert models.parse_date(value) is None


def test_date_layer_does_not_raise_on_numeric_deadline():
    # 绕过进门那层，直接考验日期那一层：拿到非字符串就返回 None，不抛。
    entry = models.item_from_record(item())
    entry.deadline = 20261101
    assert models.days_left(entry, _dt.date(2026, 10, 25)) is None
    assert models.is_open(entry, _dt.date(2026, 10, 25)) is True
    assert models.urgency(entry, _dt.date(2026, 10, 25)) == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-01", (2026, 11, 1)),
        ("2026/11/1", (2026, 11, 1)),
        ("2026年11月1日", (2026, 11, 1)),
        ("2026-11-01T09:30:00+08:00", (2026, 11, 1)),
        ("20261101", (2026, 11, 1)),
    ],
)
def test_dates_parse_into_year_month_day(text, expected):
    parsed = models.parse_date(text)
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == expected


@pytest.mark.parametrize(
    "text", ["", "   ", "不是日期", "2026-02-30", "2026-13-45", "2026-11"]
)
def test_unparsable_dates_are_none_never_a_guess(text):
    assert models.parse_date(text) is None


# ------------------------------------------------------------------ 去重


@pytest.mark.parametrize(
    "title", ["", "   ", "【通知】", "【公告】", "转发：", "　　"]
)
def test_empty_after_normalisation_means_empty_fingerprint(title):
    assert models.normalize_title(title) == ""
    assert models.title_fingerprint(title) == ""


def test_four_empty_titles_stay_four_items(tmp_path):
    """4 条指纹为空的标题必须各自独立：4 条进、4 条出。

    这些标题写出来都不是空的（【通知】【公告】转发：（通知）），
    是归一化之后才变空的 —— 指纹为空，但它们是四条互不相干的东西。
    """
    data = tmp_path / "data"
    titles = ["【通知】", "【公告】", "转发：", "（通知）"]
    assert all(models.title_fingerprint(title) == "" for title in titles)
    rows = [
        models.item_from_record({"id": f"src:{index}", "title": title})
        for index, title in enumerate(titles)
    ]
    assert all(row is not None for row in rows)
    stats = store.merge_items(data, rows)
    assert stats.added == 4
    assert stats.merged == 0
    assert len(store.read_items(data)) == 4


def test_a_missing_title_is_not_the_same_thing_as_a_blank_one():
    """「必填」管的是归一之后有没有值，不管它归一化之后会不会变空。"""
    assert models.item_from_record({"id": "a:1", "title": "【通知】"}) is not None
    assert models.item_from_record({"id": "a:2", "title": ""}) is None
    assert models.item_from_record({"id": "a:3", "title": "   "}) is None
    assert models.item_from_record({"id": "a:4"}) is None


def test_normal_titles_get_distinct_fingerprints():
    titles_ = ["中华人民共和国甲法（草案）", "中华人民共和国乙法（草案）", "某某条例"]
    prints = [models.title_fingerprint(title) for title in titles_]
    assert len(set(prints)) == len(titles_)
    assert "" not in prints


def test_full_width_and_half_width_brackets_are_the_same_title():
    assert models.title_fingerprint("示例法（草案）") == models.title_fingerprint("示例法(草案)")


def test_empty_fingerprint_rows_never_enter_the_dedup_map(tmp_path):
    data = tmp_path / "data"
    store.merge_items(data, [models.item_from_record({"id": "src:x", "title": "【通知】"})])
    # 更新那条路：同 id、标题仍然是空指纹
    stats = store.merge_items(
        data, [models.item_from_record({"id": "src:x", "title": "【公告】", "issuer": "甲"})]
    )
    assert stats.updated == 1
    # 新来一条空标题条目，不能被并进上面那条
    stats = store.merge_items(data, [models.item_from_record({"id": "src:y", "title": "转发："})])
    assert stats.added == 1
    assert stats.merged == 0
    assert len(store.read_items(data)) == 2


def test_same_title_from_two_sources_merges(tmp_path):
    data = tmp_path / "data"
    store.merge_items(
        data,
        [models.item_from_record({"id": "人大:abc", "title": "同一份文件", "source_urls": ["https://a.cn/1"]})],
    )
    stats = store.merge_items(
        data,
        [models.item_from_record({"id": "国务院:def", "title": "同一份文件", "source_urls": ["https://b.cn/2"]})],
    )
    assert stats.added == 0
    assert stats.merged == 1
    assert len(store.read_items(data)) == 1
    stored = store.read_items(data)[0]
    assert stored.id == "人大:abc"
    assert stored.source_urls == ["https://a.cn/1", "https://b.cn/2"]


def test_different_titles_never_merge(tmp_path):
    data = tmp_path / "data"
    store.merge_items(data, [models.item_from_record({"id": "a:1", "title": "甲法草案"})])
    stats = store.merge_items(data, [models.item_from_record({"id": "b:2", "title": "乙法草案"})])
    assert stats.added == 1
    assert stats.merged == 0
    assert len(store.read_items(data)) == 2


def test_same_source_repeat_updates_by_id_and_keeps_old_fields(tmp_path):
    data = tmp_path / "data"
    store.merge_items(
        data,
        [models.item_from_record({"id": "人大:abc", "title": "同一份文件", "level": "法律"})],
    )
    stats = store.merge_items(
        data, [models.item_from_record({"id": "人大:abc", "title": "同一份文件", "issuer": "全国人大"})]
    )
    assert stats.merged == 0
    assert stats.updated == 1
    stored = store.read_items(data)[0]
    assert stored.level == "法律"
    assert stored.issuer == "全国人大"


def test_merge_sweeps_existing_duplicates(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    rows = [
        {
            "id": "旧:1",
            "title": "存量重复的文件",
            "first_seen": "2026-01-01T00:00:00",
            "source_urls": ["https://old.cn/1"],
        },
        {
            "id": "新:2",
            "title": "存量重复的文件",
            "first_seen": "2026-05-05T00:00:00",
            "source_urls": ["https://new.cn/2"],
        },
    ]
    (data / "items.jsonl").write_bytes(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8") + b"\n"
    )
    stats = store.merge_items(data, [models.item_from_record({"id": "无关:3", "title": "不相干的草案"})])
    assert stats.merged == 1
    entries = by_id(data)
    assert set(entries) == {"旧:1", "无关:3"}
    assert entries["旧:1"].source_urls == ["https://old.cn/1", "https://new.cn/2"]


def test_manual_closed_survives_a_resync(tmp_path):
    data = tmp_path / "data"
    store.merge_items(data, [models.item_from_record(item())])
    assert store.set_user_closed(data, "src:aaa", True) is True
    stats = store.merge_items(data, [models.item_from_record(item())])
    stored = store.read_items(data)[0]
    assert stored.user_closed is True
    assert stored.user_closed_at
    assert stats.unchanged == 1


def test_manual_closed_beats_everything(tmp_path):
    data = tmp_path / "data"
    future = (_dt.date.today() + _dt.timedelta(days=30)).isoformat()
    store.merge_items(data, [models.item_from_record(item(deadline=future))])
    assert store.set_user_closed(data, "src:aaa", True) is True
    stored = store.read_items(data)[0]
    assert models.is_open(stored) is False
    assert models.urgency(stored) == -1


def test_undo_writes_false_back_to_disk(tmp_path):
    data = tmp_path / "data"
    store.merge_items(data, [models.item_from_record(item())])
    store.set_user_closed(data, "src:aaa", True)
    assert store.set_user_closed(data, "src:aaa", False) is True
    stored = store.read_items(data)[0]
    assert stored.user_closed is False
    assert stored.user_closed_at == ""
    assert store.set_user_closed(data, "不存在:id", True) is False


# ------------------------------------------------------------------ 并发


def test_concurrent_writers_do_not_lose_items(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    errors = []

    def worker(worker_id):
        try:
            for n in range(8):
                store.merge_items(
                    data,
                    [models.item_from_record({"id": f"w{worker_id}:{n}", "title": f"线程 {worker_id} 的第 {n} 份"})],
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors
    assert len(store.read_items(data)) == 24


def test_stale_lock_is_broken(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    lock = data / store.LOCK_NAME
    lock.write_bytes(b"")
    old = time.time() - (store.LOCK_STALE_SECONDS + 60)
    os.utime(lock, (old, old))
    stats = store.merge_items(data, [models.item_from_record(item())])
    assert stats.added == 1
    assert not lock.exists()


def test_failed_write_leaves_no_tmp_debris(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.merge_items(data, [models.item_from_record(item())])
    assert list(data.glob("*.tmp")) == []


def test_tmp_name_carries_the_process_id(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(pathlib.Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    path = tmp_path / "items.jsonl"
    store.write_bytes_atomic(path, b"[]\n")
    assert seen
    assert str(os.getpid()) in seen[0]


def test_writes_are_atomic(tmp_path, monkeypatch):
    path = tmp_path / "items.jsonl"
    store.write_bytes_atomic(path, b"OLD")
    observed = []
    real_replace = os.replace

    def spy(src, dst):
        observed.append(path.read_bytes())
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.write_bytes_atomic(path, b"NEW")
    assert observed == [b"OLD"]
    assert path.read_bytes() == b"NEW"


def test_written_files_use_lf_only(root):
    path = write_index(root / "in.json", [item(), item("src:bbb", "另一部草案")])
    assert run_cli(["sync", "--from", str(path)], root)[0] == 0
    for name in ("items.jsonl", "runs.jsonl"):
        assert b"\r\n" not in (root / "data" / name).read_bytes()
    assert b"\r\n" not in (root / "board.html").read_bytes()
