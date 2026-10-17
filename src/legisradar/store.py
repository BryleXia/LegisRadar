"""本地库的读写。

两个文件：``items.jsonl`` 一行一条条目，``runs.jsonl`` 一次同步记一条。
看板只读这两个。

写法上有三条硬要求：

* **读—改—写整段在锁里**。读在锁外的话，那道缝里别人合并进来的条目会被
  手里这份旧快照整段盖掉，而两边都报成功。
* **原子落盘**：先写临时文件再 replace。临时文件名带进程号 —— 共用一个固定的
  ``.tmp`` 名字时，两个写者会撞在一起，Windows 上抛 PermissionError，
  Unix 上是安静的互相覆盖（后者更糟，丢数据且不出声）。
* **用 bytes 写**。文本模式在 Windows 上会把每个换行翻成回车换行，
  这条转换没有开关能拦住。
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import models

log = logging.getLogger("legisradar")

ITEMS_NAME = "items.jsonl"
RUNS_NAME = "runs.jsonl"
LOCK_NAME = ".lock"

# 锁文件存在超过这个秒数就当成上个进程被强杀留下的残骸拆掉；
# 否则一次崩溃会把库永久锁死。
LOCK_STALE_SECONDS = 300
_LOCK_POLL_SECONDS = 0.02

# 坏行要报，但一份被截断的文件可能坏掉几千行，逐行报没有意义。
MAX_REPORTED_BAD_LINES = 5


@dataclass
class MergeStats:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    merged: int = 0


@dataclass
class Library:
    items: list[models.Item] = field(default_factory=list)
    bad_lines: int = 0


def items_path(data_dir: Path) -> Path:
    return data_dir / ITEMS_NAME


def runs_path(data_dir: Path) -> Path:
    return data_dir / RUNS_NAME


def tmp_path_for(path: Path) -> Path:
    """临时文件的名字。带进程号 —— 两个写者不能撞在同一个名字上。"""
    return path.with_name(f"{path.name}.{os.getpid()}.tmp")


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """先写临时文件再 replace。失败时不留残骸。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def _lock(data_dir: Path):
    """进程间互斥。锁文件用 O_CREAT|O_EXCL 建，过旧的当残骸拆掉。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / LOCK_NAME
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                # 锁刚好被上一个持有者放掉，立刻重试。
                continue
            if age > LOCK_STALE_SECONDS:
                log.debug("拆掉过旧的锁文件（已存在 %.0f 秒）", age)
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            time.sleep(_LOCK_POLL_SECONDS)
            continue
        else:
            os.close(handle)
            break
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def _read_lines(path: Path) -> list[bytes | None]:
    """按行读字节。整行解不出 UTF-8 时那一位是 None。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("读不了 %s：%s", path, exc)
        return []

    lines: list[bytes | None] = []
    for chunk in raw.split(b"\n"):
        line = chunk.strip()
        if not line:
            continue
        try:
            line.decode("utf-8")
        except UnicodeDecodeError:
            lines.append(None)
            continue
        lines.append(line)
    return lines


def read_library(data_dir: Path) -> Library:
    """读条目库。一行坏数据不能让整库读不出来 —— 坏行跳过并告警，其余照常。"""
    path = items_path(data_dir)
    library = Library()
    for line in _read_lines(path):
        if line is None:
            library.bad_lines += 1
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            library.bad_lines += 1
            if library.bad_lines <= MAX_REPORTED_BAD_LINES:
                log.warning("%s 里有一行读不懂，已跳过：%s", path.name, exc)
            continue
        item = models.item_from_record(record, local=True)
        if item is None:
            library.bad_lines += 1
            if library.bad_lines <= MAX_REPORTED_BAD_LINES:
                log.warning("%s 里有一行不是能用的条目，已跳过", path.name)
            continue
        library.items.append(item)
    if library.bad_lines > MAX_REPORTED_BAD_LINES:
        log.warning(
            "%s 里共 %d 行读不出来，已跳过", path.name, library.bad_lines
        )
    return library


def read_items(data_dir: Path) -> list[models.Item]:
    return read_library(data_dir).items


def read_runs(data_dir: Path) -> list[dict]:
    """读同步记录。形状不对的行按「没有」处理，不让调用方抛。"""
    path = runs_path(data_dir)
    runs: list[dict] = []
    for line in _read_lines(path):
        if line is None:
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(record, dict):
            runs.append(record)
    return runs


def last_run(data_dir: Path) -> dict | None:
    runs = read_runs(data_dir)
    return runs[-1] if runs else None


def _serialize(items) -> bytes:
    lines = [
        json.dumps(item.to_record(), ensure_ascii=False, sort_keys=True) for item in items
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def _serialize_runs(runs) -> bytes:
    lines = [json.dumps(run, ensure_ascii=False, sort_keys=True) for run in runs]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def _union(old: list[str], new: list[str]) -> list[str]:
    out = list(old)
    for value in new:
        if value not in out:
            out.append(value)
    return out


def _absorb(old: models.Item, new: models.Item) -> None:
    """把新数据逐字段并进已有条目。

    某个字段新数据是空（空串 / 空列表 / 空字典）而旧数据有值，就保留旧的：
    数据是分次补齐的，整条替换会让补齐过的信息一次次被打回空白。
    列表型字段取并集：同一份文件可能被多个站点发，链接应该累积。
    """
    # related 也在这一组里：跑批重算出来的相近条目要能盖掉上一轮那份。
    for name in ("title", "issuer", "level", "published", "deadline", "source_status", "related"):
        value = getattr(new, name)
        if value:
            setattr(old, name, value)
    old.source_urls = _union(old.source_urls, new.source_urls)
    old.tags = _union(old.tags, new.tags)
    if new.stats:
        merged = dict(old.stats)
        for key, value in new.stats.items():
            if value is None:
                continue
            if isinstance(value, (str, list, dict)) and not value:
                continue
            merged[key] = value
        old.stats = merged
    if new.user_closed:
        # 单向闩：人工判定是人对机器的纠正，重拉绝不能洗掉它。
        old.user_closed = True
        if new.user_closed_at:
            old.user_closed_at = new.user_closed_at
    # 指纹每次从标题重算：存着的那份是缓存，归一化规则一改就会漂移。
    old.fingerprint = models.title_fingerprint(old.title)


def _sort_key(item: models.Item) -> tuple:
    return (item.first_seen or "9999-99-99", item.id)


def _dedupe_library(items: list[models.Item]) -> tuple[list[models.Item], int]:
    """扫一遍全库清掉存量重复：指纹相同的只留 first_seen 最早的那条。

    空指纹不参与这个扫描 —— 它们本来就是互不相干的条目。
    返回（清理后的条目，被并掉的条数）。
    """
    groups: dict[str, list[models.Item]] = {}
    for item in items:
        item.fingerprint = models.title_fingerprint(item.title)
        if not item.fingerprint:
            continue
        groups.setdefault(item.fingerprint, []).append(item)

    dropped: set[str] = set()
    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=_sort_key)
        keep = group[0]
        for other in group[1:]:
            _absorb(keep, other)
            dropped.add(other.id)
            merged += 1
    kept = [item for item in items if item.id not in dropped]
    kept.sort(key=_sort_key)
    return kept, merged


def merge_items(
    data_dir: Path, incoming: list[models.Item], *, now: str | None = None
) -> MergeStats:
    """把新拉到的条目并进库里。整段读—改—写在锁里。"""
    stats = MergeStats()
    stamp = now or _dt.datetime.now().isoformat(timespec="seconds")

    with _lock(data_dir):
        existing = read_library(data_dir).items
        by_id: dict[str, models.Item] = {item.id: item for item in existing}
        by_fingerprint: dict[str, models.Item] = {}
        for item in existing:
            item.fingerprint = models.title_fingerprint(item.title)
            if item.fingerprint and item.fingerprint not in by_fingerprint:
                by_fingerprint[item.fingerprint] = item

        for new in incoming:
            fingerprint = models.title_fingerprint(new.title)
            new.fingerprint = fingerprint

            old = by_id.get(new.id)
            if old is None:
                # 库里没有这个 id：先看是不是别处已经收过同一份文件。
                # 空指纹不参与这一步 —— 把空串当正常键去查表，
                # 互不相干的条目会被当成同一份文件全并成一条。
                if fingerprint and fingerprint in by_fingerprint:
                    _absorb(by_fingerprint[fingerprint], new)
                    stats.merged += 1
                    continue
                if not new.first_seen:
                    new.first_seen = stamp
                by_id[new.id] = new
                if fingerprint:
                    by_fingerprint[fingerprint] = new
                stats.added += 1
                continue

            # 同一个 id 再次到手：这是更新，不算跨来源合并。
            if old.content_key() == new.content_key():
                stats.unchanged += 1
                if new.user_closed and not old.user_closed:
                    old.user_closed = True
                    old.user_closed_at = new.user_closed_at or old.user_closed_at
                continue

            previous = old.fingerprint
            _absorb(old, new)
            stats.updated += 1
            if previous and previous != old.fingerprint and by_fingerprint.get(previous) is old:
                del by_fingerprint[previous]
            if old.fingerprint and old.fingerprint not in by_fingerprint:
                by_fingerprint[old.fingerprint] = old

        kept, cleaned = _dedupe_library(list(by_id.values()))
        stats.merged += cleaned
        write_bytes_atomic(items_path(data_dir), _serialize(kept))

    return stats


def record_run(
    data_dir: Path, *, at: str, index_generated_at: str, source: str
) -> None:
    """追加一条同步记录。"""
    record = {"at": at, "index_generated_at": index_generated_at, "source": source}
    with _lock(data_dir):
        runs = read_runs(data_dir)
        runs.append(record)
        write_bytes_atomic(runs_path(data_dir), _serialize_runs(runs))


def set_user_closed(
    data_dir: Path, item_id: str, closed: bool, *, now: str | None = None
) -> bool:
    """写下一条人工判定。条目不在库里就返回 False。

    撤销是显式的人工动作，走这条路直写落盘 —— 不走 merge，
    因为 merge 的门闩会让 False 永远写不进去。
    """
    stamp = now or _dt.datetime.now().isoformat(timespec="seconds")
    with _lock(data_dir):
        items = read_library(data_dir).items
        found = False
        for item in items:
            if item.id != item_id:
                continue
            found = True
            item.user_closed = bool(closed)
            item.user_closed_at = stamp if closed else ""
            break
        if not found:
            return False
        write_bytes_atomic(items_path(data_dir), _serialize(items))
    return True


def count_items(data_dir: Path) -> int:
    return len(read_items(data_dir))
