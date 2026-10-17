"""本地小服务。

看板以 ``file://`` 打开时，浏览器里的脚本物理上写不了文件 —— 这不是权限开关
能解决的。所以「已截止」这个动作交给一个小服务。

**只听 127.0.0.1。** 服务跑在每个人自己的机器上，关掉窗口即停止。
但用户浏览器里打开的其他网页可以朝 localhost 发请求（CSRF），所以有两道闸：
Host 校验与 Token。写请求必须带 ``X-Serve-Token``；看板本体不带校验 ——
它是 token 的投递通道，给它加校验等于把钥匙锁在门里。

日志就是用户眼前那个黑窗口。客户端提前断开（刷新、连点两下、关标签页）是常态
不是故障，只记一行 debug；真正的程序错误说成一句人话，完整回溯留给 -v。
"""

from __future__ import annotations

import http.server
import json
import logging
import re
import secrets
import urllib.parse

from . import board as board_module
from . import store

log = logging.getLogger("legisradar")

HOST = "127.0.0.1"
MAX_BODY_BYTES = 1024 * 1024
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
CLOSED_PATH = re.compile(r"^/api/items/(?P<item>[^/]+)/user-closed$")

ERR_HOST = "Host 不合法"
ERR_TOKEN = "缺少有效的 X-Serve-Token（CSRF 防护）"
ERR_NOT_FOUND = "not found"
ERR_NO_BOARD = "board.html 不存在，先跑一轮 legisradar board 生成它"
ERR_NO_PLACEHOLDER = "看板模板缺少 serve-token 占位（版本不匹配？）"
ERR_LENGTH = "请求体长度不合法"
ERR_JSON = "请求体不是合法的 JSON 对象"
ERR_CLOSED_FIELD = '需要 {"closed": true|false}'


def new_token() -> str:
    """每次启动新生成一枚 token：32 个十六进制字符。"""
    return secrets.token_hex(16)


def token_meta(token: str) -> str:
    return f'<meta name="legisradar-serve-token" content="{token}">'


class Handler(http.server.BaseHTTPRequestHandler):
    """两个端点：看板本体，以及给条目点「已截止」。"""

    protocol_version = "HTTP/1.1"
    server_version = "legisradar"
    sys_version = ""

    # 请求级流水只记 debug：黑窗口里不该出现正常访问的记录。
    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        self._guard(self._handle_get)

    def do_POST(self):  # noqa: N802
        self._guard(self._handle_post)

    # ---------------------------------------------------------------- 内部

    def _guard(self, handler):
        try:
            handler()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as exc:
            log.debug("客户端提前断开：%s: %s", type(exc).__name__, exc)
        except Exception as exc:  # noqa: BLE001 - 任何畸形输入都要走完「回一条错误」
            log.warning("处理请求出错：%s: %s", type(exc).__name__, exc)
            log.debug("完整回溯", exc_info=True)
            try:
                self._send_json(500, {"error": "服务内部错误"})
            except Exception:
                log.debug("错误响应也没能发出去", exc_info=True)

    def _host_ok(self) -> bool:
        raw = self.headers.get("Host") or ""
        # 浏览器访问 http://127.0.0.1:8765/ 时 Host 头就是 127.0.0.1:8765，
        # 比对之前必须先去掉端口。
        host = raw.split(":")[0].strip().lower()
        return host in ALLOWED_HOSTS

    def _token_ok(self) -> bool:
        given = self.headers.get("X-Serve-Token") or ""
        return secrets.compare_digest(given, self.server.token)

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_get(self) -> None:
        if not self._host_ok():
            self._send_json(403, {"error": ERR_HOST})
            return
        if self._path() not in ("/", "/board.html"):
            self._send_json(404, {"error": ERR_NOT_FOUND})
            return

        path = self.server.cfg.board
        try:
            # 现读现注入：出图之后刷新一下就能看到新的。
            html = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._send_json(404, {"error": ERR_NO_BOARD})
            return

        if board_module.TOKEN_META not in html:
            self._send_json(500, {"error": ERR_NO_PLACEHOLDER})
            return
        html = html.replace(board_module.TOKEN_META, token_meta(self.server.token), 1)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _handle_post(self) -> None:
        if not self._host_ok():
            self._send_json(403, {"error": ERR_HOST})
            return
        match = CLOSED_PATH.match(self._path())
        if match is None:
            self._send_json(404, {"error": ERR_NOT_FOUND})
            return
        if not self._token_ok():
            self._send_json(403, {"error": ERR_TOKEN})
            return

        # int() 必须包在 try 里：Content-Length: abc 会抛 ValueError，
        # 它从这里逃出去的话连接会被直接关掉、一个响应都没有。
        try:
            length = int(self.headers.get("Content-Length"))
        except (TypeError, ValueError):
            self._send_json(400, {"error": ERR_LENGTH})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": ERR_LENGTH})
            return

        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": ERR_JSON})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": ERR_JSON})
            return
        closed = payload.get("closed")
        if not isinstance(closed, bool):
            self._send_json(400, {"error": ERR_CLOSED_FIELD})
            return

        item_id = urllib.parse.unquote(match.group("item"))
        if not store.set_user_closed(self.server.cfg.data_dir, item_id, closed):
            self._send_json(404, {"error": f"条目不存在：{item_id}"})
            return

        try:
            board_module.write_board(self.server.cfg)
        except Exception as exc:  # noqa: BLE001
            # 落盘和出图是两件事。判定已经写进库里了 —— 这时回错误、或者让异常
            # 逃出去掐断连接，都是在说假话：用户以为没生效，于是再点一次，
            # 而第二次点下去是反向操作，正好把刚做成的判定抹掉。
            log.warning("看板重建失败（判定已落库）：%s: %s", type(exc).__name__, exc)
            log.debug("完整回溯", exc_info=True)

        self._send_json(200, {"ok": True, "id": item_id, "closed": closed})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 allow_reuse_address 映射成 SO_REUSEADDR，语义是允许两个进程
    # 绑同一个端口：第二个进程永远拿不到流量，却打印得和第一个一样。
    # 关掉之后，第二个进程会老老实实拿到「端口被占用」。
    allow_reuse_address = False

    def __init__(self, address, handler, *, cfg, token):
        self.cfg = cfg
        self.token = token
        super().__init__(address, handler)

    def handle_error(self, request, client_address):
        # 默认把它打到 stderr 上。这个程序的 stderr 留给调用者看，不写栈。
        log.debug("请求处理异常（%s）", client_address, exc_info=True)


def make_server(port: int, cfg, token: str) -> Server:
    return Server((HOST, port), Handler, cfg=cfg, token=token)


def bind_server(port: int, cfg, token: str, tries: int = 20) -> tuple[Server, int]:
    """从 port 开始往后找第一个能用的端口，返回 (服务, 实际端口)。

    为什么不让用户自己去改配置：这张看板默认 8765，而维护者那边的运维页
    也用同一个默认端口 —— 两个程序同时开着时，后启动的那个必然起不来。
    为这件事去改 config.toml、记住改过哪里、以后再改回来，成本远高于
    程序自己往后挪一格，反正浏览器会跟着新地址打开。

    只往后挪，不随机：随机端口会让人下次找不到自己刚开的那个页面。
    """
    last: OSError | None = None
    for candidate in range(int(port), int(port) + int(tries) + 1):
        try:
            return Server((HOST, candidate), Handler, cfg=cfg, token=token), candidate
        except OSError as exc:
            last = exc
    assert last is not None
    raise last


def serve_forever(server: Server) -> None:
    server.serve_forever(poll_interval=0.2)
