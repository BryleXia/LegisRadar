"""十一、启动器（Windows 双击的那个）。"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import socket
import subprocess

import pytest

from conftest import ROOT

BAT = ROOT / "启动.bat"
CMD = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
# 「这台机器上没有 Python」的最小 PATH。只能放 System32 ——
# C:\Windows 底下住着 py.exe，把它算进来就变成「装了 py 启动器」的场景了。
MINIMAL_PATH = str(pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")

pytestmark = pytest.mark.skipif(os.name != "nt", reason="启动.bat 是 Windows 的东西")


def stage_tree(tmp_path: pathlib.Path, config_text: str) -> pathlib.Path:
    """把分发树的一小份拷进临时目录：启动器、配置、src。

    绝不在仓库目录里跑启动器 —— 它会留下 data/ 与 board.html。
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    shutil.copyfile(BAT, dist / "启动.bat")
    shutil.copytree(ROOT / "src", dist / "src")
    (dist / "config.toml").write_text(config_text, encoding="utf-8")
    return dist


def run_bat(dist: pathlib.Path, args=(), path_value: str | None = None):
    env = dict(os.environ)
    if path_value is not None:
        env["PATH"] = path_value
    # 必须给完整路径：cmd 用相对名找非 ASCII 的批处理会报「不是内部或外部命令」，
    # 而双击（资源管理器）走的不是这条路径，所以那不代表启动器坏了。
    command = [str(CMD), "/c", str(dist / "启动.bat"), *args]
    result = subprocess.run(
        command,
        cwd=str(dist),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=300,
    )
    return result.returncode, result.stdout.decode("gb18030", errors="replace")


def run_bat_until(dist: pathlib.Path, seconds: float = 30, args=(), path_value: str | None = None):
    """跑启动器，到时间就杀掉。

    serve 现在的行为是「端口被占就往后挪一格，然后一直服务」——
    它不会再自己退出。启动器跑完 serve 也停在 `pause`，所以这里只能
    等到时间再收尸，不能用 subprocess.run 的 timeout（它拿不到半截输出）。
    """
    env = dict(os.environ)
    # 输出被接进管道时 Python 会改成块缓冲，serve 那几行要等缓冲区满才出来。
    # 这里要的是「跑起来就能看到」，不是吞吐。
    env["PYTHONUNBUFFERED"] = "1"
    if path_value is not None:
        env["PATH"] = path_value
    command = [str(CMD), "/c", str(dist / "启动.bat"), *args]
    proc = subprocess.Popen(
        command, cwd=str(dist), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        out, _ = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        # ★ 必须杀**进程树**。proc.kill() 只杀掉 cmd.exe，它下面那个
        #   `python -m legisradar serve` 还活着、还攥着 stdout 管道 ——
        #   随后 communicate() 会一直等管道关闭，测试挂在原地。
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = b""
    return proc.returncode, (out or b"").decode("gb18030", errors="replace")


def occupied_port() -> tuple[socket.socket, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


CONFIG = """\
[general]
data_dir = "data"
board = "board.html"

[viewer]
index_url = ""

[serve]
port = {port}
"""


def test_launcher_uses_the_system_python(tmp_path):
    blocker, port = occupied_port()
    try:
        dist = stage_tree(tmp_path, CONFIG.format(port=port))
        code, out = run_bat_until(dist)
    finally:
        blocker.close()

    assert "版本过低" not in out
    assert "没找到 Python" not in out
    assert "应用商店的占位符" not in out
    assert "使用系统 Python" in out or "使用 py 启动器" in out
    # 认出来之后要真的往下走：出看板、起服务。
    assert (dist / "board.html").is_file()
    assert "看板已更新" in out
    # 端口被占时 serve 往后挪一格，而不是退出。
    assert f"端口 {port} 已被占用，改用" in out


def test_launcher_passes_arguments_through_to_sync(tmp_path):
    blocker, port = occupied_port()
    try:
        dist = stage_tree(tmp_path, CONFIG.format(port=port))
        index = dist / "index.json"
        index.write_text(
            '{"schema": 2, "generated_at": "2026-10-10T08:00:00", "generator": "测试夹具",'
            ' "count": 1, "items": [{"id": "a:1", "title": "随包索引里的一条"}]}',
            encoding="utf-8",
        )
        code, out = run_bat_until(dist, args=("--from", "index.json"))
    finally:
        blocker.close()
    assert "新增 1" in out
    assert "随包索引里的一条" in (dist / "board.html").read_text(encoding="utf-8")


def test_launcher_without_any_python_only_gives_guidance(tmp_path):
    # 端口先占住：万一这台机器上还是被找到了某个 Python，启动器会走到 serve
    # 那一步并立刻拿到「端口被占用」，而不是把整个测试挂在那里。
    blocker, port = occupied_port()
    try:
        dist = stage_tree(tmp_path, CONFIG.format(port=port))
        code, out = run_bat(dist, path_value=MINIMAL_PATH)
    finally:
        blocker.close()
    assert code == 1
    assert "python.org" in out
    assert "只做检测" in out
    for phrase in ("正在下载", "开始安装", "自动安装", "即将安装"):
        assert phrase not in out
    assert "版本过低" not in out
    assert "没找到 Python" in out
    # 它不会真的去动这台机器
    assert not (dist / "board.html").exists()


def test_launcher_never_mistakes_a_working_python_for_an_old_one(tmp_path):
    """好 Python 被判成「版本过低」是反的判断写法的典型后果。"""
    source = BAT.read_bytes().decode("gb18030")
    executable = [
        line
        for line in source.splitlines()
        if line.strip() and not line.strip().lower().startswith("rem")
    ]
    for line in executable:
        assert "!errorlevel!==" not in line.replace(" ", "")
    # 真跑一遍：能用的 Python 不该被说成版本过低
    blocker, port = occupied_port()
    try:
        dist = stage_tree(tmp_path, CONFIG.format(port=port))
        code, out = run_bat_until(dist)
    finally:
        blocker.close()
    assert "版本过低" not in out


def test_launcher_reports_a_missing_board_instead_of_starting_the_service(tmp_path):
    dist = stage_tree(tmp_path, CONFIG.format(port=8765))
    (dist / "src").rename(dist / "src-hidden")  # 让 board 一定跑不起来
    code, out = run_bat(dist, path_value=os.environ.get("PATH", ""))
    assert "看板未生成，请检查上方日志。" in out
    assert not (dist / "board.html").exists()
