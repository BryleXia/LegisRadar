"""命令行解析与四个命令的实现。

命令集就是 sync / board / serve / status 四个。退出码是设计的一部分：
断网但本地有数据时 sync 返回 0（看板照常出），只有真没辙时才返回 1。

控制台上出现的符号只用 GBK 里有的那几个（√ × ※ ★）。stdout 被重定向进文件或
管道时，Python 改按本地代码页编码，一个不在 GBK 里的字符就会让整条命令以
UnicodeEncodeError 中断 —— 而直连控制台时它走控制台 API，什么都印得出来。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import logging
import sys
import webbrowser

from . import __version__, board, config, models, serve, store, sync

log = logging.getLogger("legisradar")

PROG = "legisradar"
DESCRIPTION = "立法雷达（查看器）— 法规草案征求意见，一份本地看板"

HELP_SYNC = "拉取最新的条目索引（唯一的联网命令）"
HELP_BOARD = "只重新生成看板（不联网）"
HELP_SERVE = "起本地服务：页面上可给条目点「已截止」"
HELP_STATUS = "查看本地条目状态"

CONFIG_BROKEN_SYNC = (
    "  先把 config.toml 修好再同步 —— 否则会对着一个空库同步，看起来像数据全丢了。"
)
CONFIG_BROKEN_SERVE = "  先把 config.toml 修好再起服务。"
NO_INDEX = (
    '× 没有索引地址。在 config.toml 里填 [viewer] index_url = "..."，'
    "或临时用 legisradar sync --from <地址或本地文件>。"
)
ALL_INVALID_HEAD = "× 索引里有 {total} 条，但没有一条能用 —— 多半是格式变了。"
ALL_INVALID_TAIL = (
    "  本地库一个字都没动（也不记这一轮同步）。这说明程序该更新了（去仓库下新版）。"
)
NO_LOCAL_DATA = "  本地一条数据都没有，看板会是空的。"
HAVE_LOCAL_DATA = "  本地还有 {count} 条上一次同步的数据，看板照常出（日期是旧的）。"
EMPTY_INDEX_HEAD = "※ 这份索引是空的（0 条），本地还有 {count} 条。"
EMPTY_INDEX_TAIL = (
    "  如果这不是你预期的，说明发布方那一步可能出了问题："
    "看板会在「索引汇总于」那里显示这份空索引的生成时间，而条目还是上一轮的。"
)
SKIPPED = "※ 索引里有 {count} 条读不懂或不合规，已跳过。"
UNREACHABLE_HEAD = "每一个索引地址都拉不到："

BUNDLED_NAME = "index.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=DESCRIPTION)
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印更多细节")

    subs = parser.add_subparsers(dest="command")

    p_sync = subs.add_parser("sync", help=HELP_SYNC, description=HELP_SYNC)
    p_sync.add_argument(
        "--from",
        dest="source",
        default=None,
        help="索引地址或本地文件（默认取 [viewer] index_url）",
    )

    subs.add_parser("board", help=HELP_BOARD, description=HELP_BOARD)

    p_serve = subs.add_parser("serve", help=HELP_SERVE, description=HELP_SERVE)
    p_serve.add_argument(
        "--port", type=int, default=None, help="端口（默认取 [serve] port，缺省 8765）"
    )
    p_serve.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    subs.add_parser("status", help=HELP_STATUS, description=HELP_STATUS)
    return parser


def _setup_logging(verbose: bool) -> None:
    """日志走 stdout —— 这个程序的日志就是用户眼前那个黑窗口。

    stderr 留给调用者，本程序不往里写任何东西。
    """
    logger = logging.getLogger("legisradar")
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_legisradar_stdout", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler._legisradar_stdout = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


def _tolerant_stdout() -> None:
    """重定向出去时，库里的字符不该让整条命令以 UnicodeEncodeError 中断。"""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")


# ------------------------------------------------------------------ sync


def _bundled_stamp(path) -> _dt.date | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(65536)
        envelope = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(envelope, dict):
        return None
    stamp = envelope.get("generated_at")
    if not isinstance(stamp, str) or len(stamp) < 10:
        return None
    return models.parse_date(stamp[:10])


def _fetch_reason(failures) -> str:
    if len(failures) == 1:
        address, reason, standalone = failures[0]
        return reason if standalone else f"{address} 拉不到：{reason}"
    lines = [UNREACHABLE_HEAD]
    for address, reason, standalone in failures:
        lines.append(f"    {reason}" if standalone else f"    {address} —— {reason}")
    return "\n".join(lines)


def _absorb_bundled(cfg) -> bool:
    """把随包的 index.json 吃进来。没有、坏了、空的都返回 False。

    ★ 只在**本地一条都没有**时调用。本地有数据时，这份旧快照只会把用户
      手里更新的东西盖回去 —— 那不是救急，是倒退。

    这件事原来是让用户自己做的（打印一行 `--from index.json` 让他复制）。
    可那份索引就躺在同一个文件夹里 —— 断网、第一次打开、本地全空，
    三个条件同时成立时，让程序自己读它比让人照着屏幕敲命令更可靠。
    """
    path = cfg.root / BUNDLED_NAME
    if not path.is_file():
        return False
    try:
        parsed = sync.parse_index(sync.read_source(str(path)))
    except (sync.IndexUnavailable, sync.IndexRejected) as exc:
        print(f"  随包的 {BUNDLED_NAME} 也用不了：{exc}")
        return False
    if not parsed.items:
        return False

    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    store.merge_items(cfg.data_dir, parsed.items, now=stamp)
    store.record_run(
        cfg.data_dir,
        at=stamp,
        index_generated_at=parsed.generated_at,
        source=f"随包索引 {BUNDLED_NAME}",
    )

    when = ""
    day = _bundled_stamp(path)
    if day is not None:
        age = (_dt.date.today() - day).days
        when = "（就是今天的）" if age <= 0 else f"（{age} 天前的）"
    generated = parsed.generated_at or "生成时间未知"
    print(f"  先用这个文件夹里随包的 {BUNDLED_NAME}：{generated}{when}，{len(parsed.items)} 条。")
    print("  下次联网再跑一次，就会换成最新的。")
    return True


def _report_unavailable(reason: str, local_count: int, cfg) -> int:
    print(f"※ 这次没同步上：{reason}")
    absorbed = False
    if not local_count:
        absorbed = _absorb_bundled(cfg)
    _regenerate_board(cfg)
    if absorbed:
        # 退出码说的是「有没有东西可看」，不是「网络成没成」——
        # 这条契约写在 start.sh 里：本地有数据就是 0。刚补上的这份就是数据。
        return 0
    if local_count:
        print(HAVE_LOCAL_DATA.format(count=local_count))
        return 0
    print(NO_LOCAL_DATA)
    return 1


def _regenerate_board(cfg) -> None:
    """没同步上时也把看板重出一遍：手里那份旧数据照常能看。

    出不来不是这一轮的重点（这一轮本来就失败了），记一行 debug 就好。
    """
    try:
        board.write_board(cfg)
    except Exception as exc:  # noqa: BLE001
        log.debug("看板没重出来：%s: %s", type(exc).__name__, exc)


def cmd_sync(args) -> int:
    cfg = config.load()
    if cfg.error:
        print(f"× {cfg.error}")
        print(CONFIG_BROKEN_SYNC)
        return 1

    local_count = len(store.read_items(cfg.data_dir))

    if args.source:
        # --from 是用户点名要的来源：它失败时不试镜像，
        # 那等于背着他去连另一个主机。
        candidates = [args.source]
    else:
        if not cfg.index_url:
            print(NO_INDEX)
            return 1
        candidates = [cfg.index_url, *cfg.index_mirrors]

    failures = []
    parsed = None
    used = ""
    for candidate in candidates:
        try:
            data = sync.read_source(candidate)
        except sync.IndexUnavailable as exc:
            failures.append((candidate, exc.reason, exc.standalone))
            continue
        except sync.IndexRejected as exc:
            # 来源本身取得到，但那份东西整份不能用 —— 换地址也换不来一份好的。
            return _report_unavailable(str(exc), local_count, cfg)
        try:
            parsed = sync.parse_index(data)
        except sync.IndexRejected as exc:
            return _report_unavailable(str(exc), local_count, cfg)
        used = candidate
        break

    if parsed is None:
        return _report_unavailable(_fetch_reason(failures), local_count, cfg)

    if parsed.total and not parsed.items:
        print(ALL_INVALID_HEAD.format(total=parsed.total))
        print(ALL_INVALID_TAIL)
        return 1

    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    stats = store.merge_items(cfg.data_dir, parsed.items, now=stamp)
    store.record_run(
        cfg.data_dir,
        at=stamp,
        index_generated_at=parsed.generated_at,
        source=used,
    )

    print(f"√ 索引：{parsed.generated_at} 由 {parsed.generator} 生成")
    print(f"  来源 {used}")
    print(
        f"  共 {len(parsed.items)} 条 → 新增 {stats.added} · 更新 {stats.updated}"
        f" · 无变化 {stats.unchanged} · 合并重复 {stats.merged}"
    )
    try:
        out = board.write_board(cfg)
    except (board.BoardError, OSError) as exc:
        print(f"× {exc}")
        return 1
    print(f"  看板已更新：{out}")

    if parsed.total == 0:
        print()
        print(EMPTY_INDEX_HEAD.format(count=len(store.read_items(cfg.data_dir))))
        print(EMPTY_INDEX_TAIL)
    elif parsed.skipped:
        print()
        print(SKIPPED.format(count=parsed.skipped))
    return 0


# ----------------------------------------------------------------- board


def cmd_board(args) -> int:
    # 只读命令：配置读不懂时硬跑，用默认值。
    cfg = config.load()
    try:
        out = board.write_board(cfg)
    except (board.BoardError, OSError) as exc:
        print(f"× {exc}")
        return 1
    print(f"看板已更新：{out}（共 {store.count_items(cfg.data_dir)} 条）")
    return 0


# ---------------------------------------------------------------- status


def cmd_status(args) -> int:
    cfg = config.load()
    items = store.read_items(cfg.data_dir)
    today = _dt.date.today()
    open_count = sum(1 for item in items if models.is_open(item, today))
    soon = sorted(
        (
            (models.days_left(item, today), item)
            for item in items
            if models.is_due_soon(item, today)
        ),
        key=lambda pair: pair[0],
    )

    print(f"立法雷达（查看器）v{__version__}")
    print(f"  条目总数 {len(items)} · 在征 {open_count} · 7 天内截止 {len(soon)}")
    print(f"  数据目录 {cfg.data_dir}")
    if soon:
        print()
        print("  ★ 7 天内截止：")
        for days, item in soon:
            print(f"     [{days:>2}天] {item.title[:50]}")
    return 0


# ----------------------------------------------------------------- serve


def cmd_serve(args) -> int:
    cfg = config.load()
    if cfg.error:
        print(f"× {cfg.error}")
        print(CONFIG_BROKEN_SERVE)
        return 1

    requested = args.port if args.port is not None else cfg.port
    token = serve.new_token()
    try:
        server, port = serve.bind_server(requested, cfg, token)
    except OSError as exc:
        print(f"  端口 {requested} 往后试了 20 个，都起不来（{exc}）。")
        print("  看看还有没有别的立法雷达窗口没关，或者用 --port 指定一个更大的端口。")
        return 1
    if port != requested:
        print(f"  端口 {requested} 已被占用，改用 {port}。")

    # 用真正绑上的那个端口：--port 0 时系统会挑一个，报配置里的值就成了假话。
    url = f"http://{serve.HOST}:{server.server_address[1]}/"
    print(f"  本地服务已启动：{url}")
    print("  在这个页面里可以给条目点「已截止」。关闭本窗口即停止服务。")
    if not args.no_open:
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    try:
        serve.serve_forever(server)
    except KeyboardInterrupt:
        print("\n  服务已停止。")
    finally:
        server.server_close()
    return 0


# ------------------------------------------------------------------ main

COMMANDS = {
    "sync": cmd_sync,
    "board": cmd_board,
    "serve": cmd_serve,
    "status": cmd_status,
}


def main(argv=None) -> int:
    _tolerant_stdout()
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1

    _setup_logging(bool(getattr(args, "verbose", False)))

    command = getattr(args, "command", None)
    if not command:
        # 不带子命令不是「无事发生」：打印帮助并返回 1。
        parser.print_help()
        return 1
    return COMMANDS[command](args)
