"""从索引拉条目 —— 本程序唯一的网络行为。

索引是一份 JSON 信封，也是唯一的外部数据来源。它放在公开仓库里，谁都能提
改动，所以进门这层把每一处都当不可信：schema 严格判类型、条目逐字段归一、
未知字段忽略、只留 http(s) 链接。

两条路都受同一个大小上限约束 —— 「服务器返了 1GB」和「本地有份 1GB」后果一样。
上限取值 32 MB：真实索引一百多 KB，留两三个数量级的余量，同时挡住意外。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, models

log = logging.getLogger("legisradar")

SCHEMA = 2
MAX_INDEX_BYTES = 32 * 1024 * 1024
# 单次请求超时。同一个地址不重试 —— 备用地址本来就是为「某个地址不通」准备的，
# 在一个地址上重试只会让用户多等。
REQUEST_TIMEOUT = 20
USER_AGENT = f"legisradar/{__version__} (+https://github.com/BryleXia/legisradar)"


class IndexUnavailable(Exception):
    """取不到索引：网络不通、地址不通、文件不存在、既不是地址也不是文件。"""

    def __init__(self, reason: str, *, standalone: bool = False):
        super().__init__(reason)
        self.reason = reason
        # 这句话本身已经写清了是哪个来源，不需要再套一层「地址 —— 原因」。
        self.standalone = standalone


class IndexRejected(Exception):
    """索引取到了，但整份不能用：不是 JSON、schema 不对、超上限、没有 items。"""


@dataclass
class ParsedIndex:
    generated_at: str = ""
    generator: str = ""
    items: list[models.Item] = field(default_factory=list)
    total: int = 0
    skipped: int = 0


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _brief(value) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} 字节"


def _too_big(size: int) -> str:
    return f"索引有 {format_size(size)}，超过 {MAX_INDEX_BYTES // (1024 * 1024)} MB 上限"


def looks_like_url(value: str) -> bool:
    lowered = (value or "").strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _read_url(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    header_size = int(declared)
                except (TypeError, ValueError):
                    header_size = -1
                if header_size > MAX_INDEX_BYTES:
                    raise IndexRejected(_too_big(header_size))
            data = response.read(MAX_INDEX_BYTES + 1)
    except IndexRejected:
        raise
    except urllib.error.HTTPError as exc:
        raise IndexUnavailable(f"HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise IndexUnavailable(str(exc.reason)) from exc
    except OSError as exc:
        raise IndexUnavailable(str(exc)) from exc
    if len(data) > MAX_INDEX_BYTES:
        raise IndexRejected(_too_big(len(data)))
    return data


def _read_file(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise IndexUnavailable(f"读不了 {path}：{exc}") from exc
    if size > MAX_INDEX_BYTES:
        raise IndexRejected(_too_big(size))
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_INDEX_BYTES + 1)
    except OSError as exc:
        raise IndexUnavailable(f"读不了 {path}：{exc}") from exc
    if len(data) > MAX_INDEX_BYTES:
        raise IndexRejected(_too_big(len(data)))
    return data


def read_source(source: str) -> bytes:
    """按来源取回索引的原始字节。"""
    if looks_like_url(source):
        return _read_url(source)
    path = Path(source)
    if path.is_file():
        return _read_file(path)
    raise IndexUnavailable(
        f"既不是本机存在的文件，也不是 http/https 地址：{source}\n"
        "    （如果它是个网址，是不是漏了 https:// ？）",
        standalone=True,
    )


def parse_index(data: bytes) -> ParsedIndex:
    """校验信封、把条目逐条归一。整份不合规就抛 IndexRejected。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IndexRejected(f"索引不是 UTF-8 文本（{exc}）") from exc
    try:
        envelope = json.loads(text)
    except ValueError as exc:
        raise IndexRejected(f"索引不是合法的 JSON：{exc}") from exc

    if not isinstance(envelope, dict):
        raise IndexRejected("索引顶层不是一个对象")

    schema = envelope.get("schema")
    # 用 == 的话 1.0 和 true 都会被放行（Python 里 True == 1）。
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        raise IndexRejected(f"索引的 schema 是 {_brief(schema)}，这份程序只认 {SCHEMA}")

    rows = envelope.get("items")
    if not isinstance(rows, list):
        rows = None
    if rows is None:
        raise IndexRejected("索引里没有 items 列表")

    parsed = ParsedIndex(
        generated_at=_text(envelope.get("generated_at")),
        generator=_text(envelope.get("generator")),
        total=len(rows),
    )

    not_objects = 0
    invalid = 0
    for row in rows:
        if not isinstance(row, dict):
            not_objects += 1
            continue
        item = models.item_from_record(row, local=False)
        if item is None:
            invalid += 1
            continue
        parsed.items.append(item)

    if not_objects:
        # 不能只是静静滤掉：用户会以为整份索引就这么几条。
        log.warning("索引里有 %d 条不是对象，已忽略", not_objects)
    parsed.skipped = not_objects + invalid
    return parsed
