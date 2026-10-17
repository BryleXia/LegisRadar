"""条目模型。

这里放四样东西，每一样全程序只有一份：

* 法律位阶清单（看板的筛选顺序从它来）
* 标题归一化与指纹（去重的依据）
* 日期解析与剩余天数
* 「在征 / 已截止」的判定顺序（看板的三组与 status 的计数共用它）

两层归一化的分工是固定的：进门那一层只把类型摆正（不合规的值降级成空，
数字转成字符串字面量），日期那一层只负责解析（只认字符串，认不出返回 None）。
两层都不抛异常 —— 库里有一条读不懂的日期，不该让整库看起来是坏的。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

# 法律位阶。表里没有的取值不进这个表 —— 看板把它们按字母序排在已知层级之后。
LEVELS = ("法律", "行政法规", "司法解释", "部门规章", "立法计划")
LEVEL_RANK = {name: index for index, name in enumerate(LEVELS)}

# 一个门类标签，筛选条上排最后 —— 不是程序造出来的兜底桶。
OTHER_TAG = "其他"

SOURCE_STATUSES = ("", "open", "closed")

# 归一化之后落进这里就是「没有信息量的标题」，指纹为空、不参与去重。
_EMPTY_TITLES = frozenset(
    {"通知", "公告", "转发", "转载", "通知公告", "公告通知", "通知转发"}
)
# 整串被一对括号包着时去掉那对括号：公告标题常这么写。
_WRAPPER = re.compile(r"^[\[\(【][^\]\)】]*[\]\)】]$")
# 「转发：」「转载:」这类前缀不承载区分信息。
_PREFIX = re.compile(r"^(转发|转载)\s*[:：]?\s*")
# 去掉首尾的标点与空白；标题内部的所有空白也一并去掉。
_EDGE_PUNCT = ":：·—-－—、,，.。;；|/\\'\"“”‘’「」『』【】[]()（）<>《》 \t　"
_SPACE = re.compile(r"\s+")

_RE_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})")
_RE_SPLIT = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_RE_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
_RE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ]")


def _as_text(value, *, strip: bool = True) -> str:
    """把值摆正成字符串。

    不是字符串、也不是数字的一律成空串；布尔不算数字 —— True 变成 "True"
    只会伪造出一个看起来像日期或标题的值。
    """
    if isinstance(value, str):
        return value.strip() if strip else value
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _as_list(value) -> list[str]:
    """列表型字段归一到真列表：不是列表就落成空列表，元素只留非空字符串。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        text = _as_text(entry)
        if text and text not in out:
            out.append(text)
    return out


def _as_dict(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_bool(value) -> bool:
    return value is True


def is_http_url(value) -> bool:
    """只认 http(s)：其余协议（javascript:、data:、vbscript: …）一律不当作链接。"""
    if not isinstance(value, str):
        return False
    lowered = value.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


# 浏览器解析 URL 时会剥掉 ASCII 制表符、换行与控制字符，所以下面必须先剥一遍，
# 不能只去首尾 —— 只去首尾的话，中间那个 tab 会让「有没有协议」的判断匹配不上。
_URL_STRIP = re.compile(r"[\s\x00-\x1f\x7f]")
_URL_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def sanitize_url(value) -> str:
    """链接的渲染闸门（渲染前自己拦的那一道）。

    放行 http(s)、相对路径、Windows 盘符；其余协议一律成空串。
    以两个斜杠开头的先挡掉：``//evil.com/x`` 看着像站内路径，浏览器会补成
    ``https://evil.com/x``；反斜杠开头的同理（Windows 上还会触发一次 SMB 连接）。
    """
    if not isinstance(value, str):
        return ""
    cleaned = _URL_STRIP.sub("", value)
    if not cleaned:
        return ""
    # 反斜杠在浏览器眼里等同斜杠，判开头时按同一个字符处理。
    if cleaned[:2].replace("\\", "/").startswith("//"):
        return ""
    lowered = cleaned.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return cleaned
    if _URL_DRIVE.match(cleaned):
        return cleaned
    if _URL_SCHEME.match(cleaned):
        return ""
    return cleaned


def normalize_title(text) -> str:
    """标题归一化：全角折半角、去掉包装括号与转发前缀、去掉全部空白。"""
    if not isinstance(text, str):
        return ""
    result = unicodedata.normalize("NFKC", text).strip()
    for _ in range(3):
        stripped = _WRAPPER.sub("", result).strip()
        if stripped == result:
            break
        result = stripped
    result = _PREFIX.sub("", result).strip()
    result = result.strip(_EDGE_PUNCT)
    result = _SPACE.sub("", result)
    if result in _EMPTY_TITLES:
        return ""
    return result


def title_fingerprint(title) -> str:
    """标题指纹。归一化之后为空的标题指纹是空串，空串不参与去重。"""
    normalized = normalize_title(title)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def parse_date(value) -> _dt.date | None:
    """解析日期。只认字符串；认不出（含类型不对）一律返回 None，绝不猜。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = _RE_COMPACT.fullmatch(text)
    if match is None:
        match = _RE_SPLIT.fullmatch(text)
    if match is None:
        match = _RE_CN.fullmatch(text)
    if match is None:
        match = _RE_ISO.match(text)
    if match is None:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None


def format_stamp(value) -> str:
    """时间戳的显示形式：ISO 里的 T 换成空格，截到 16 个字符。"""
    text = _as_text(value)
    if not text:
        return ""
    return text.replace("T", " ")[:16]


@dataclass
class Item:
    """一条法规草案征求意见公告。

    前十二个字段来自索引；后三个只归本地库，索引里带了也不认。
    """

    id: str
    title: str = ""
    fingerprint: str = ""
    issuer: str = ""
    level: str = ""
    published: str = ""
    deadline: str = ""
    source_urls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_status: str = ""
    stats: dict = field(default_factory=dict)
    # 维护者跑批时算好的「语义相近的条目 id」。索引里没这个键就是空列表。
    related: list[str] = field(default_factory=list)
    user_closed: bool = False
    user_closed_at: str = ""
    first_seen: str = ""

    def content_key(self) -> tuple:
        """比对「内容有没有变」用的键。不含只归本地库的那三项。"""
        return (
            self.title,
            self.issuer,
            self.level,
            self.published,
            self.deadline,
            tuple(self.source_urls),
            tuple(self.tags),
            self.source_status,
            json.dumps(self.stats, sort_keys=True, ensure_ascii=False, default=str),
            tuple(self.related),
        )

    def to_record(self) -> dict:
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "issuer": self.issuer,
            "level": self.level,
            "published": self.published,
            "deadline": self.deadline,
            "source_urls": list(self.source_urls),
            "tags": list(self.tags),
            "source_status": self.source_status,
            "stats": dict(self.stats),
            "related": list(self.related),
            "user_closed": self.user_closed,
            "user_closed_at": self.user_closed_at,
            "first_seen": self.first_seen,
        }


def item_from_record(raw, *, local: bool = False) -> Item | None:
    """把一条记录摆正成 Item。

    ``local=False`` 用来读索引：``user_closed`` / ``user_closed_at`` /
    ``first_seen`` 三样是索引里带了也不认的，整个滤掉。
    ``id`` 与 ``title`` 都是必填，归一之后缺任何一个就返回 None ——
    没有 id 的条目没法用，没有标题的条目没法读。
    """
    if not isinstance(raw, dict):
        return None

    item_id = _as_text(raw.get("id"))
    if not item_id:
        return None

    title = _as_text(raw.get("title"))
    if not title:
        return None
    source_status = _as_text(raw.get("source_status")).lower()
    if source_status not in SOURCE_STATUSES:
        source_status = ""

    links = [url for url in _as_list(raw.get("source_urls")) if is_http_url(url)]

    item = Item(
        id=item_id,
        title=title,
        fingerprint=title_fingerprint(title),
        issuer=_as_text(raw.get("issuer")),
        level=_as_text(raw.get("level")),
        published=_as_text(raw.get("published")),
        deadline=_as_text(raw.get("deadline")),
        source_urls=links,
        tags=_as_list(raw.get("tags")),
        source_status=source_status,
        stats=_as_dict(raw.get("stats")),
        related=_as_list(raw.get("related")),
    )

    if local:
        item.user_closed = _as_bool(raw.get("user_closed"))
        item.user_closed_at = _as_text(raw.get("user_closed_at"))
        item.first_seen = _as_text(raw.get("first_seen"))

    return item


def days_left(item: Item, today: _dt.date | None = None) -> int | None:
    """剩余天数 = 截止日期减今天，按本地日期只取整天。没有截止日期就是 None。"""
    due = parse_date(item.deadline)
    if due is None:
        return None
    return (due - (today or _dt.date.today())).days


def is_open(item: Item, today: _dt.date | None = None) -> bool:
    """一条是否仍在征求意见期。判据的顺序是固定的，前面成立就不再往后看。"""
    if item.user_closed:
        # 人工判定是人看过原文后的结论，机器的任何推断都不该推翻它。
        return False
    if item.source_status == "closed":
        # 来源方自己标的「已结束」比日期更可信。
        return False
    remaining = days_left(item, today)
    if remaining is None:
        # 没有截止日期、来源方也没标记：保守地按「仍在进行」。宁可多提醒，不可漏提醒。
        return True
    # 截止当天仍然算在征。
    return remaining >= 0


def is_due_soon(item: Item, today: _dt.date | None = None) -> bool:
    """是否在征且 7 天内（含当天）截止。"""
    if not is_open(item, today):
        return False
    remaining = days_left(item, today)
    return remaining is not None and 0 <= remaining <= 7


def urgency(item: Item, today: _dt.date | None = None) -> int:
    """条目行的紧急度，取值 3 / 2 / 0 / -1，直接写进 data-urg。"""
    if not is_open(item, today):
        return -1
    remaining = days_left(item, today)
    if remaining is None:
        return 0
    if remaining <= 3:
        return 3
    if remaining <= 7:
        return 2
    return 0
