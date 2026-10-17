"""测试夹具与工具。

每个测试都在自己的临时目录里造一个「程序根」：配置、数据目录、看板路径
全都相对它解析。仓库里不放任何运行时产物。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import pathlib
import socket
import sys
import threading
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from legisradar import board, cli, config, models, serve, store, sync  # noqa: E402

DEFAULT_TOML = """\
[general]
data_dir = "data"
board = "board.html"

[viewer]
index_url = ""
index_mirrors = []

[serve]
port = 8765
"""


# ------------------------------------------------------------------ 程序根


def write_config(root: pathlib.Path, text: str) -> pathlib.Path:
    path = root / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    """一个带出厂配置的空程序根。"""
    write_config(tmp_path, DEFAULT_TOML)
    return tmp_path


@pytest.fixture
def bare_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """连 config.toml 都没有的程序根。"""
    return tmp_path


# ------------------------------------------------------------------ 跑命令


def run_cli(argv, root: pathlib.Path, extra_env: dict | None = None):
    """在进程内跑一条命令，返回 (退出码, stdout, stderr)。"""
    env = {"LEGISRADAR_ROOT": str(root)}
    if extra_env:
        env.update(extra_env)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def patched_root(root: pathlib.Path):
    with mock.patch.dict(os.environ, {"LEGISRADAR_ROOT": str(root)}):
        yield


# ------------------------------------------------------------------ 日志


class Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):  # noqa: D102
        self.records.append(record)

    @property
    def messages(self):
        return [record.getMessage() for record in self.records]

    def levels(self, name):
        return [record for record in self.records if record.levelname == name]


@contextlib.contextmanager
def capture_logs(level=logging.DEBUG):
    """接住 legisradar 这个 logger 上的一切记录。"""
    logger = logging.getLogger("legisradar")
    collector = Collector()
    logger.addHandler(collector)
    previous = logger.level
    logger.setLevel(min(level, previous if previous else level))
    try:
        yield collector
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous)


# ------------------------------------------------------------------ 索引


def item(
    ident="src:aaa",
    title="中华人民共和国示例法（草案）",
    **extra,
) -> dict:
    record = {"id": ident, "title": title}
    record.update(extra)
    return record


def index_blob(items=None, *, schema=2, generated_at="2026-10-10T08:00:00", generator="测试夹具", **extra) -> str:
    envelope = {
        "schema": schema,
        "generated_at": generated_at,
        "generator": generator,
        "count": len(items or []),
        "items": [] if items is None else items,
    }
    envelope.update(extra)
    return json.dumps(envelope, ensure_ascii=False)


def write_index(path: pathlib.Path, items=None, **kwargs) -> pathlib.Path:
    path.write_text(index_blob(items, **kwargs), encoding="utf-8")
    return path


def sync_from(root: pathlib.Path, index_path: pathlib.Path, *extra):
    return run_cli(["sync", "--from", str(index_path), *extra], root)


# ------------------------------------------------------------------ 网络


def dead_url() -> str:
    """一个没人听的地址：连上去立刻被拒。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/index.json"


@contextlib.contextmanager
def http_server(responses):
    """起一个本机小服务。responses 是 [(状态码, 正文)] 或 [(状态码, 原始字节)]。

    请求数超过预定条数时，最后一条会被重复使用。
    """
    import http.server

    planned = list(responses)
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            index = min(len(seen), len(planned)) - 1
            status, body = planned[index]
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.json", seen
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ 服务


@contextlib.contextmanager
def running_server(cfg, token):
    server = serve.make_server(0, cfg, token)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------------------ 看板


def make_items(records, *, local=False):
    out = []
    for record in records:
        parsed = models.item_from_record(record, local=local)
        assert parsed is not None
        out.append(parsed)
    return out


def render_board(cfg, records, *, runs=(), local=False, today=None) -> str:
    return board.render(make_items(records, local=local), list(runs), cfg, today=today)


__all__ = [
    "Collector",
    "DEFAULT_TOML",
    "ROOT",
    "SRC",
    "board",
    "capture_logs",
    "cli",
    "config",
    "dead_url",
    "http_server",
    "index_blob",
    "item",
    "make_items",
    "models",
    "patched_root",
    "render_board",
    "run_cli",
    "running_server",
    "serve",
    "store",
    "sync",
    "sync_from",
    "write_config",
    "write_index",
]
