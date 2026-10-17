"""看板出图。

产物是**单文件 HTML**：CSS 与 JS 全部内联，数据以一段
``<script type="application/json">`` 嵌进去，一行外部链接都没有 ——
目标读者在中国大陆，任何外部 CDN 都可能慢或不通，而这是一个双击就打开的本机工具。

数据处理全在这里做完，模板里那段脚本只负责画。这样「在征还是已截止」这类
判定只有 models 那一份，模板不会养出第二份。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

from . import __version__, models

log = logging.getLogger("legisradar")

TEMPLATE_NAME = "board.html"
DATA_PLACEHOLDER = "__LEGISRADAR_DATA__"
# 服务模式靠这段 meta 的 content 是否为空来判：静态文件里是空的，
# 由本地服务在应答时填入本次启动生成的 token。
TOKEN_META = '<meta name="legisradar-serve-token" content="">'

# 看板上的逐字文案。改这里一处，页面上所有出现的地方一起改。
STRINGS = {
    "no_data": (
        "还没有同步到数据。多半是网络不通，而这个文件夹里也没有可用的 "
        "index.json —— 把一份索引放进来，或者联网后重新运行启动脚本。"
    ),
    "no_live_tail": "已经截止的条目仍然列着，点标题可以打开官网。",
    "no_link": "本条目没有可打开的原文链接。",
    "freshness": "数据新鲜度",
    "data_at": "索引汇总于",
    "last_run": "上次同步",
    "refresh_hint": "看板不会自己更新 —— 重新运行一次启动脚本就会去拉最新的条目索引。",
    "heartbeat_none": "还没有同步过",
    "heartbeat_ok": "上次同步 ",
    "serve_hint": (
        "这个看板是静态 HTML 文件 —— 想给条目点「已截止」，"
        "请用启动脚本打开（它会起本地服务）。"
    ),
    "footer_source": "数据来自各机关官网公开发布的征求意见公告，汇总成一份条目索引。",
    "about_tail": (
        "这是个信息聚合站 —— 只负责让你知道有哪些法规在征求意见，不替你提交意见。"
        "条目索引由维护者定期汇总各部委官网公开发布的公告；本程序只读取这份索引，"
        "也不转载正文。"
    ),
}

# 版面上的其余固定说法。同样只在这里定义一次。
TEXT = {
    "subtitle": "全国人大 · 国务院各部委 · 两高 —— 法规草案公开征求意见",
    "summary_open": "件在征",
    "summary_soon": "件 7 天内截止",
    "summary_closed": "件已截止",
    "all_levels": "全部层级",
    "group_soon": "即将截止（7 天内）",
    "group_soon_note": "优先处理这些",
    "group_open": "其余在征",
    "group_closed": "已截止",
    "group_closed_note": "窗口已关闭，官网通常也不再提供全文",
    "unit_day": "天",
    "no_deadline": "—",
    "open_link": "查看原文",
    "mark_closed": "不再关注",
    "mark_reopen": "恢复关注",
    "confirm_dismiss": "确定把这条移入「已截止」栏吗？之后还可以恢复。",
    "save_failed": "没保存上，再点一次",
    "manual_mark": "不再关注",
    "comments": "已有 {n} 条意见",
    "deadline_prefix": "截止 ",
    "date_arrow": " → ",
    "empty_filtered_title": "没有符合当前筛选的条目",
    "empty_filtered_body": "试试清空搜索框或取消标签筛选。",
    "empty_open_title": "当前没有在征的法规草案",
    "empty_open_body": (
        "这是正常状态 —— 人大全年只有约 5~6 个月有征求意见窗口"
        "（每两个月一批、每批 30 天）。"
    ),
    "search_placeholder": "搜索标题、发文机关、门类",
    "about_button": "关于",
    "about_title": "关于这张看板",
    "about_when": "这份数据是什么时候的",
    "about_where": "你的数据在哪",
    "about_where_note": "备份就是把 {dir} 目录整个拷走。",
    "about_version": "程序版本",
    "about_close": "关闭",
    "count_label": "条数",
    "index_missing": "（索引里没写）",
    "fresh_ok": "正常",
    "fresh_warn": "偏旧",
    "fresh_bad": "很旧",
    "theme_light": "浅色",
    "theme_dark": "深色",
    "related_summary": "语义相关的还有 {n} 条",
    "related_gone": "（这条已不在库中）",
    "related_today": "今天截止",
    "related_days": "还剩 {n} 天",
    "related_none": "无期限",
}

# 心跳圆点的三档。这个工具跟 30 天的征求意见窗口赛跑，索引大约一周更新一次：
# 一周多没更新值得提醒一次，一个月没更新基本等于这份数据已经没用。
HEARTBEAT_WARN_DAYS = 8
HEARTBEAT_BAD_DAYS = 30


class BoardError(Exception):
    """看板出不来：模板缺了、模板读不到。"""


def template_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / TEMPLATE_NAME


def read_template() -> str:
    path = template_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BoardError(f"读不到看板模板 {path}：{exc}") from exc


def _age_days(stamp, today: _dt.date) -> int | None:
    text = models.format_stamp(stamp)
    if len(text) < 10:
        return None
    day = models.parse_date(text[:10])
    if day is None:
        return None
    return (today - day).days


def _heartbeat(run: dict | None, today: _dt.date) -> tuple[str, str]:
    """返回（圆点状态, 状态旁的文字）。没同步过时圆点状态是空串。"""
    if not run:
        return "", STRINGS["heartbeat_none"]
    age = _age_days(run.get("at"), today)
    if age is None or age < HEARTBEAT_WARN_DAYS:
        level = "ok"
    elif age < HEARTBEAT_BAD_DAYS:
        level = "warn"
    else:
        level = "bad"
    return level, STRINGS["heartbeat_ok"] + models.format_stamp(run.get("at"))


def _freshness_word(level: str) -> str:
    return {
        "ok": TEXT["fresh_ok"],
        "warn": TEXT["fresh_warn"],
        "bad": TEXT["fresh_bad"],
    }.get(level, STRINGS["heartbeat_none"])


def _comments_of(item: models.Item):
    value = item.stats.get("comments")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    return None


def _date_phrase(item: models.Item) -> str:
    published = item.published
    deadline = item.deadline
    if published and deadline:
        return published + TEXT["date_arrow"] + deadline
    if deadline:
        return TEXT["deadline_prefix"] + deadline
    if published:
        return published
    return ""


def _row(item: models.Item, today: _dt.date) -> dict:
    days = models.days_left(item, today)
    opened = models.is_open(item, today)
    # 渲染前自己再拦一道：条目里的链接来自索引，而索引是外部输入。
    # 只有过闸之后仍然是 http(s) 的才当作链接（相对路径与盘符不成链接）。
    links = [
        safe
        for safe in (models.sanitize_url(url) for url in item.source_urls)
        if models.is_http_url(safe)
    ]
    meta = []
    if item.issuer:
        meta.append({"k": "text", "v": item.issuer})
    if item.level:
        meta.append({"k": "lvl", "v": item.level})
    phrase = _date_phrase(item)
    if phrase:
        meta.append({"k": "date", "v": phrase})
    haystack = " ".join([item.title, item.issuer, item.level, *item.tags]).lower()
    return {
        "id": item.id,
        "title": item.title,
        "level": item.level,
        "days": days,
        "urg": models.urgency(item, today),
        "state": "open" if opened else "closed",
        "meta": meta,
        "haystack": haystack,
        "comments": _comments_of(item),
        "manual": bool(item.user_closed),
        "tags": list(item.tags),
        "related": list(item.related),
        "links": links,
        "href": links[0] if links else "",
    }


def _sort_open(row: dict) -> tuple:
    # 没有截止日期的垫底。
    days = row["days"]
    return (days is None, days if days is not None else 0, row["title"], row["id"])


def _sort_closed(row: dict) -> tuple:
    # 最近截止的在前，日期未知的垫底。
    days = row["days"]
    return (days is None, -(days if days is not None else 0), row["title"], row["id"])


def _tag_order(counts: dict) -> list[dict]:
    ordered = sorted(
        counts.items(),
        key=lambda pair: (pair[0] == models.OTHER_TAG, -pair[1], pair[0]),
    )
    return [{"name": name, "count": count} for name, count in ordered]


def _shown_path(raw: str, name: str) -> str:
    """把文件名接到配置里写下的那个目录后面。

    接出来的仍是配置里那个样子：写的是相对路径就还是相对路径，
    用反斜杠写的 Windows 路径也不会在这里变成正斜杠。
    """
    base = raw.rstrip("/\\")
    sep = "\\" if "\\" in base else "/"
    return base + sep + name


def build_payload(items, runs, cfg, *, today: _dt.date | None = None) -> dict:
    today = today or _dt.date.today()
    rows = [_row(item, today) for item in items]

    soon_rows = sorted(
        [r for r in rows if r["state"] == "open" and r["days"] is not None and r["days"] <= 7],
        key=_sort_open,
    )
    rest_rows = sorted(
        [
            r
            for r in rows
            if r["state"] == "open"
            and not (r["days"] is not None and r["days"] <= 7)
        ],
        key=_sort_open,
    )
    closed_rows = sorted([r for r in rows if r["state"] == "closed"], key=_sort_closed)

    counts: dict[str, int] = {}
    for item in items:
        for tag in item.tags:
            counts[tag] = counts.get(tag, 0) + 1

    present = [name for name in models.LEVELS if any(item.level == name for item in items)]
    extra = sorted(
        {item.level for item in items if item.level and item.level not in models.LEVEL_RANK}
    )

    run = runs[-1] if runs else None
    level, heartbeat_text = _heartbeat(run, today)

    index_at = TEXT["index_missing"]
    run_at = STRINGS["heartbeat_none"]
    if run:
        run_at = models.format_stamp(run.get("at")) or STRINGS["heartbeat_none"]
        index_at = models.format_stamp(run.get("index_generated_at")) or TEXT["index_missing"]
    else:
        index_at = STRINGS["heartbeat_none"]

    return {
        "version": __version__,
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "strings": dict(STRINGS),
        "text": dict(TEXT),
        "levels": list(models.LEVELS),
        "level_chips": [TEXT["all_levels"], *present, *extra],
        "tags": _tag_order(counts),
        "summary": {
            "open": sum(1 for r in rows if r["state"] == "open"),
            "soon": len(soon_rows),
            "closed": sum(1 for r in rows if r["state"] == "closed"),
        },
        "groups": [
            {"key": "soon", "rows": soon_rows},
            {"key": "open", "rows": rest_rows},
            {"key": "closed", "rows": closed_rows},
        ],
        "library": {
            "total": len(rows),
            "has_run": run is not None,
        },
        "heartbeat": {"level": level, "text": heartbeat_text},
        "about": {
            "freshness": _freshness_word(level),
            "index_at": index_at,
            "last_run": run_at,
            "count": len(rows),
            "version": __version__,
            # 面板上的路径按配置里写的原样显示，不用解析后的绝对路径：
            # 看板会被整份转发给旁人，绝对路径顺带把本机的目录结构也带上了。
            "data_dir": cfg.data_dir_raw,
            "items_path": _shown_path(cfg.data_dir_raw, "items.jsonl"),
            "runs_path": _shown_path(cfg.data_dir_raw, "runs.jsonl"),
        },
    }


def render(items, runs, cfg, *, today: _dt.date | None = None) -> str:
    """渲染整份看板。模板缺占位符时报错，不静默出一份只有外壳的看板。"""
    template = read_template()
    if DATA_PLACEHOLDER not in template:
        raise BoardError("看板模板缺少数据占位符（版本不匹配？）")
    payload = build_payload(items, runs, cfg, today=today)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 把每一个 < 都转成 <。只转义 </ 不够：HTML 分词器还有第二条进入
    # script data double escaped 的路（<!-- 后面跟一个 <script），那时连
    # 真正用来闭合的 </script> 都不再闭合标签，整份文档会被吞掉。
    data = data.replace("<", "\\u003c")
    return template.replace(DATA_PLACEHOLDER, data)


def write_board(cfg, *, today: _dt.date | None = None) -> Path:
    """把看板写到配置指定的位置。一律 bytes + LF。"""
    from . import store

    items = store.read_items(cfg.data_dir)
    runs = store.read_runs(cfg.data_dir)
    text = render(items, runs, cfg, today=today)
    path = cfg.board
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path
