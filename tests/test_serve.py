"""九、本地服务与安全。"""

from __future__ import annotations

import http.client
import inspect
import json
import pathlib
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

from conftest import (
    ROOT,
    SRC,
    capture_logs,
    index_blob,
    item,
    run_cli,
    running_server,
    serve,
    store,
    write_index,
)
from legisradar import board, config, models

TOKEN_HEADER = "X-Serve-Token"


def call(port, method, path, body=None, headers=None):
    """发一条本地请求。

    ★ 连接建立那一步重试几次。这一条是实测补上的：整轮跑下来时，
      `test_write_with_a_wrong_token_is_refused` 偶发一次
      `ConnectionRefusedError`，单独跑这个文件永远绿、重跑整轮也绿。
      服务端在**进程内**、端口在构造时就 listen 了，所以这不是
      「server 还没起来」—— 是 Windows 上瞬时拒连。重试只包住**建连**，
      请求本身不重发：重发 POST 会让「写两次」这种 bug 变成偶发。
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    last = None
    for attempt in range(5):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.connect()
        except OSError as exc:          # 连接没建起来：可以安全重试
            conn.close()
            last = exc
            time.sleep(0.1 * (attempt + 1))
            continue
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            payload = response.read().decode("utf-8", errors="replace")
            return response.status, payload
        finally:
            conn.close()
    raise AssertionError(f"连不上 127.0.0.1:{port}（试了 5 次）：{last}") from last


def error_of(payload: str) -> str:
    return json.loads(payload)["error"]


def seed(root, count=1):
    rows = [item(f"a:{n}", f"第 {n} 份草案") for n in range(count)]
    index = write_index(root / "in.json", rows)
    assert run_cli(["sync", "--from", str(index)], root)[0] == 0
    return rows


def cfg_of(root):
    return config.load(root)


def target(item_id: str) -> str:
    return "/api/items/" + urllib.parse.quote(item_id, safe="") + "/user-closed"


def post_body(closed: bool) -> str:
    return json.dumps({"closed": closed})


# ------------------------------------------------------------------ 路由


def test_board_page_carries_the_fresh_token(root):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, payload = call(port, "GET", "/")
    assert status == 200
    assert serve.token_meta(token) in payload
    assert "application/json" in payload  # 数据块还在


def test_get_never_needs_a_token(root):
    seed(root)
    with running_server(cfg_of(root), serve.new_token()) as (_, port):
        status, _ = call(port, "GET", "/")
        status2, _ = call(port, "GET", "/board.html")
    assert status == 200
    assert status2 == 200


def test_bad_host_is_refused(root):
    seed(root)
    with running_server(cfg_of(root), serve.new_token()) as (_, port):
        status, payload = call(port, "GET", "/", headers={"Host": "evil.example"})
        status2, payload2 = call(port, "POST", target("a:0"), body=post_body(True),
                                 headers={"Host": "evil.example"})
    assert status == 403 and error_of(payload) == "Host 不合法"
    assert status2 == 403 and error_of(payload2) == "Host 不合法"


def test_host_with_a_port_is_accepted(root):
    seed(root)
    with running_server(cfg_of(root), serve.new_token()) as (_, port):
        status, _ = call(port, "GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        status2, _ = call(port, "GET", "/", headers={"Host": f"localhost:{port}"})
    assert status == 200
    assert status2 == 200


def test_unknown_paths_are_404(root):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, payload = call(port, "GET", "/nope")
        status2, payload2 = call(port, "POST", "/nope", body=post_body(True),
                                 headers={TOKEN_HEADER: token})
    assert status == 404 and error_of(payload) == "not found"
    assert status2 == 404 and error_of(payload2) == "not found"


def test_missing_board_file_is_a_404_not_a_crash(root):
    with running_server(cfg_of(root), serve.new_token()) as (_, port):
        status, payload = call(port, "GET", "/")
    assert status == 404
    assert error_of(payload) == "board.html 不存在，先跑一轮 legisradar board 生成它"


def test_board_without_the_token_placeholder_is_a_500(root):
    (root / "board.html").write_text("<html><body>没有占位符</body></html>", encoding="utf-8")
    with running_server(cfg_of(root), serve.new_token()) as (_, port):
        status, payload = call(port, "GET", "/")
    assert status == 500
    assert error_of(payload) == "看板模板缺少 serve-token 占位（版本不匹配？）"


# ------------------------------------------------------------------ 鉴权


def test_write_without_a_token_is_refused(root):
    seed(root)
    with running_server(cfg_of(root), serve.new_token()) as (_, port):
        status, payload = call(port, "POST", target("a:0"), body=post_body(True))
    assert status == 403
    assert error_of(payload) == "缺少有效的 X-Serve-Token（CSRF 防护）"


def test_write_with_a_wrong_token_is_refused(root):
    seed(root)
    with running_server(cfg_of(root), serve.new_token()) as (_, port):
        status, _ = call(port, "POST", target("a:0"), body=post_body(True),
                         headers={TOKEN_HEADER: "0" * 32})
    assert status == 403


def test_write_with_the_right_token_succeeds(root):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, payload = call(port, "POST", target("a:0"), body=post_body(True),
                               headers={TOKEN_HEADER: token})
    assert status == 200
    assert json.loads(payload)["ok"] is True


# ------------------------------------------------------------------ 写回


def test_closing_an_item_lands_in_the_library(root):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, _ = call(port, "POST", target("a:0"), body=post_body(True),
                         headers={TOKEN_HEADER: token})
    assert status == 200
    stored = store.read_items(root / "data")[0]
    assert stored.user_closed is True
    assert stored.user_closed_at
    assert models.is_open(stored) is False


def test_closing_an_unknown_item_is_404(root):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, payload = call(port, "POST", target("不存在:的条目"), body=post_body(True),
                               headers={TOKEN_HEADER: token})
    assert status == 404
    assert error_of(payload) == "条目不存在：不存在:的条目"


def test_item_ids_are_url_decoded(root):
    seed(root)
    rows = store.read_items(root / "data")
    assert rows[0].id == "a:0"
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, _ = call(port, "POST", "/api/items/a%3A0/user-closed", body=post_body(True),
                         headers={TOKEN_HEADER: token})
    assert status == 200
    assert store.read_items(root / "data")[0].user_closed is True


def test_undo_really_writes_back_to_disk(root):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        call(port, "POST", target("a:0"), body=post_body(True), headers={TOKEN_HEADER: token})
        status, _ = call(port, "POST", target("a:0"), body=post_body(False), headers={TOKEN_HEADER: token})
    assert status == 200
    stored = store.read_items(root / "data")[0]
    assert stored.user_closed is False
    assert stored.user_closed_at == ""


def test_write_survives_a_broken_board_rebuild(root, monkeypatch):
    seed(root)
    token = serve.new_token()

    def boom(*args, **kwargs):
        raise board.BoardError("模板炸了")

    monkeypatch.setattr(board, "write_board", boom)
    with capture_logs() as logs:
        with running_server(cfg_of(root), token) as (_, port):
            status, _ = call(port, "POST", target("a:0"), body=post_body(True),
                             headers={TOKEN_HEADER: token})
    assert status == 200
    assert store.read_items(root / "data")[0].user_closed is True
    assert any("看板重建失败" in message for message in logs.messages)


# ------------------------------------------------------------------ 畸形输入


def raw_request(port, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(payload)
        sock.settimeout(5)
        chunks = []
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout:
            pass
    return b"".join(chunks)


def test_bad_content_length_gets_a_response(root):
    seed(root)
    token = serve.new_token()
    request = (
        f"POST {target('a:0')} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"{TOKEN_HEADER}: {token}\r\nContent-Length: abc\r\n\r\n"
    ).encode("ascii")
    with running_server(cfg_of(root), token) as (_, port):
        response = raw_request(port, request)
    assert response.startswith(b"HTTP/1.1 400")
    assert "请求体长度不合法" in response.decode("utf-8", errors="replace")


def test_oversized_body_is_refused(root):
    # 声明的长度超过上限时，服务应当在读正文之前就停下并回一条 400。
    seed(root)
    token = serve.new_token()
    request = (
        f"POST {target('a:0')} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"{TOKEN_HEADER}: {token}\r\n"
        f"Content-Length: {serve.MAX_BODY_BYTES + 1}\r\n\r\n"
    ).encode("ascii")
    with running_server(cfg_of(root), token) as (_, port):
        response = raw_request(port, request)
    assert response.startswith(b"HTTP/1.1 400")
    assert "请求体长度不合法" in response.decode("utf-8", errors="replace")


@pytest.mark.parametrize("body", ["不是 JSON", "{", "[]", '"abc"', "42"])
def test_body_must_be_a_json_object(root, body):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, payload = call(port, "POST", target("a:0"), body=body,
                               headers={TOKEN_HEADER: token})
    assert status == 400
    assert error_of(payload) == "请求体不是合法的 JSON 对象"


@pytest.mark.parametrize("body", ['{}', '{"closed": "true"}', '{"closed": 1}', '{"closed": null}'])
def test_body_needs_a_real_boolean(root, body):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        status, payload = call(port, "POST", target("a:0"), body=body,
                               headers={TOKEN_HEADER: token})
    assert status == 400
    assert error_of(payload) == '需要 {"closed": true|false}'


# ------------------------------------------------------------------ 锁


def test_read_modify_write_happens_inside_the_lock(root, monkeypatch):
    seed(root)
    token = serve.new_token()
    held = []
    real = store.read_library

    def spy(data_dir):
        held.append((inspect.stack()[1].function, (pathlib.Path(data_dir) / store.LOCK_NAME).exists()))
        return real(data_dir)

    monkeypatch.setattr(store, "read_library", spy)
    with running_server(cfg_of(root), token) as (_, port):
        call(port, "POST", target("a:0"), body=post_body(True), headers={TOKEN_HEADER: token})
    assert ("set_user_closed", True) in held


def test_undo_path_uses_the_same_lock(root, monkeypatch):
    seed(root)
    assert store.set_user_closed(root / "data", "a:0", True) is True
    token = serve.new_token()
    held = []
    real = store.read_library

    def spy(data_dir):
        held.append((inspect.stack()[1].function, (pathlib.Path(data_dir) / store.LOCK_NAME).exists()))
        return real(data_dir)

    monkeypatch.setattr(store, "read_library", spy)
    with running_server(cfg_of(root), token) as (_, port):
        call(port, "POST", target("a:0"), body=post_body(False), headers={TOKEN_HEADER: token})
    assert ("set_user_closed", True) in held


def test_a_held_lock_blocks_the_write_until_it_is_released(root):
    seed(root)
    token = serve.new_token()
    with running_server(cfg_of(root), token) as (_, port):
        lock = root / "data" / store.LOCK_NAME
        lock.write_bytes(b"")
        try:
            result = []

            def attempt():
                result.append(call(port, "POST", target("a:0"), body=post_body(True),
                                   headers={TOKEN_HEADER: token}))

            worker = threading.Thread(target=attempt, daemon=True)
            worker.start()
            worker.join(timeout=1.0)
            assert result == [], "锁还握着，写请求就已经完成了"
            lock.unlink()
            worker.join(timeout=20)
            assert result and result[0][0] == 200
        finally:
            if lock.exists():
                lock.unlink()


# ------------------------------------------------------------------ 启动


def test_port_in_use_shifts_and_says_so(root, monkeypatch, capsys):
    """端口被占时不再报错退出，而是往后挪一格并把新地址打出来。

    看板和运维页默认都用 8765；两个程序同时开着时，后启动的那个应当
    自己让路，而不是让用户去改 config.toml。

    这里把 serve_forever 换成「记下端口就中断」，否则 serve 会一直阻塞。
    """
    seed(root)
    blocker = serve.make_server(0, cfg_of(root), serve.new_token())
    busy = blocker.server_address[1]
    seen = {}

    def fake_serve(server):
        seen["port"] = server.server_address[1]
        raise KeyboardInterrupt

    monkeypatch.setattr(serve, "serve_forever", fake_serve)
    try:
        code, out, err = run_cli(["serve", "--port", str(busy), "--no-open"], root)
    finally:
        blocker.server_close()

    assert code == 0
    assert seen["port"] != busy, "还是绑在被占的那个端口上了"
    assert f"端口 {busy} 已被占用，改用 {seen['port']}" in out
    assert "服务已停止" in out


def test_two_servers_cannot_bind_the_same_port(root):
    first = serve.make_server(0, cfg_of(root), serve.new_token())
    port = first.server_address[1]
    try:
        with pytest.raises(OSError):
            serve.make_server(port, cfg_of(root), serve.new_token())
    finally:
        first.server_close()


def test_ctrl_c_prints_the_stop_line(root, monkeypatch):
    seed(root)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(serve, "serve_forever", interrupt)
    # ★ 端口必须显式给 0（让系统随便分一个），不能用默认的 8765。
    #   用默认端口的话，这个测试**取决于跑它那台机器的状态**：谁要是正在跑
    #   这个工具（那正是开发它的人最可能的状态），8765 就被占着，`serve`
    #   在绑端口那一步就返回 1，根本走不到 `serve_forever`，于是这里红。
    #   它在 CI 上永远是绿的 —— 因为 CI 上没有人在跑这个工具。
    code, out, err = run_cli(["serve", "--port", "0", "--no-open"], root)
    assert code == 0
    assert "本地服务已启动" in out
    assert "服务已停止。" in out


def test_serve_refuses_to_start_with_a_broken_config(root):
    # \U 不是合法的 TOML 转义，这份配置整份读不懂 —— 于是 serve 必须停下，
    # 而不是拿默认端口起服务。
    (root / "config.toml").write_text(
        '[general]\ndata_dir = "C:\\Users\\someone"\n', encoding="utf-8"
    )
    assert config.load(root).error is not None, "这份配置本来就该读不懂"
    # 同样显式 `--port 0`：这条期望的失败**原因**是配置读不懂。用默认端口的话，
    # 端口被占也会让退出码变成 1 —— 于是它**因为错的原因通过**，
    # 配置那道闸坏掉了也没人知道。
    code, out, err = run_cli(["serve", "--port", "0", "--no-open"], root)
    assert code == 1
    assert "config.toml" in out
    assert "先把 config.toml 修好再起服务。" in out


# ------------------------------------------------------------------ 日志


def test_client_disconnect_is_silent_on_stderr(root, capsys):
    (root / "board.html").write_text(
        '<meta name="legisradar-serve-token" content="">' + "填充" * 400000,
        encoding="utf-8",
    )
    request = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    with capture_logs() as logs:
        with running_server(cfg_of(root), serve.new_token()) as (_, port):
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            sock.sendall(request)
            time.sleep(0.4)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()
            time.sleep(0.6)
    assert capsys.readouterr().err == ""
    assert logs.levels("WARNING") == []
    assert logs.levels("ERROR") == []
    assert logs.levels("DEBUG"), "提前断开也要留一条 DEBUG 记录"


def test_a_real_error_logs_exactly_one_warning(root, capsys, monkeypatch):
    seed(root)
    token = serve.new_token()

    def boom(*args, **kwargs):
        raise RuntimeError("故意炸的")

    monkeypatch.setattr(store, "set_user_closed", boom)
    with capture_logs() as logs:
        with running_server(cfg_of(root), token) as (_, port):
            status, payload = call(port, "POST", target("a:0"), body=post_body(True),
                                   headers={TOKEN_HEADER: token})
    assert status == 500
    warnings = logs.levels("WARNING")
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()
    assert "故意炸的" in warnings[0].getMessage()
    assert warnings[0].exc_info is None
    assert any(record.exc_info for record in logs.levels("DEBUG"))
    assert capsys.readouterr().err == ""


# ------------------------------------------------------------------ 端到端


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_end_to_end(tmp_path):
    import os

    root = tmp_path
    (root / "config.toml").write_text(
        '[general]\ndata_dir = "data"\nboard = "board.html"\n\n[serve]\nport = 8765\n',
        encoding="utf-8",
    )
    index = write_index(root / "in.json", [item("a:0", "端到端的一条")])
    assert run_cli(["sync", "--from", str(index)], root)[0] == 0
    before = (root / "board.html").read_bytes()

    port = free_port()
    env = dict(os.environ)
    env["LEGISRADAR_ROOT"] = str(root)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "legisradar", "serve", "--port", str(port), "--no-open"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        first = process.stdout.readline()
        second = process.stdout.readline()
        assert "本地服务已启动" in first + second, first + second

        status, page = call(port, "GET", "/")
        assert status == 200
        token = re.search(r'name="legisradar-serve-token" content="([0-9a-f]+)"', page).group(1)
        assert len(token) == 32 and all(char in "0123456789abcdef" for char in token)

        status, _ = call(port, "POST", target("a:0"), body=post_body(True))
        assert status == 403

        status, _ = call(port, "POST", target("a:0"), body=post_body(True),
                         headers={TOKEN_HEADER: token})
        assert status == 200
        assert store.read_items(root / "data")[0].user_closed is True
        after = (root / "board.html").read_text(encoding="utf-8")
        assert "不再关注" in after
        assert after.encode("utf-8") != before
    finally:
        process.terminate()
        process.wait(timeout=20)
        process.stdout.close()
        process.stderr.close()


def test_token_is_generated_anew_each_time():
    tokens = {serve.new_token() for _ in range(20)}
    assert len(tokens) == 20
    for token in tokens:
        assert re.fullmatch(r"[0-9a-f]{32}", token)


class Test端口被占时往后挪:
    """看板和运维页默认都用 8765，两个程序同时开着时必然撞车。

    后启动的那个应当自己往后挪一格，而不是让用户去改 config.toml ——
    改完还得记住改过哪里、以后再改回来。
    """

    def test_被占的端口不会被硬抢(self, root):
        blocker = socket.socket()
        blocker.bind((serve.HOST, 0))
        blocker.listen(1)
        try:
            with pytest.raises(OSError):
                serve.bind_server(blocker.getsockname()[1], cfg_of(root),
                                  serve.new_token(), tries=0)
        finally:
            blocker.close()

    def test_往后找第一个能用的(self, root):
        blocker = socket.socket()
        blocker.bind((serve.HOST, 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        try:
            server, used = serve.bind_server(busy, cfg_of(root), serve.new_token(), tries=3)
            try:
                assert used != busy
                assert busy < used <= busy + 3
            finally:
                server.server_close()
        finally:
            blocker.close()
