"""一、命令行与退出码。"""

from __future__ import annotations

import json
import pathlib
import re

from conftest import ROOT, index_blob, item, run_cli, write_index


def test_version_returns_zero_without_data(bare_root):
    code, out, err = run_cli(["--version"], bare_root)
    assert code == 0
    assert "legisradar" in out


def test_no_subcommand_prints_help_and_fails(root):
    code, out, err = run_cli([], root)
    assert code == 1
    assert "sync" in out
    assert "board" in out


def test_help_lists_exactly_the_four_subcommands(root):
    code, out, err = run_cli(["--help"], root)
    assert code == 0
    listed = set(re.findall(r"^\s{4}([A-Za-z][\w-]*)\s{2,}\S", out, re.M))
    assert listed == {"sync", "board", "serve", "status"}
    # usage 里那一组也要一个不多一个不少
    group = re.search(r"\{([^}]*)\}", out)
    assert group is not None
    assert set(group.group(1).split(",")) == {"sync", "board", "serve", "status"}


def test_unknown_subcommand_is_not_a_traceback(root):
    code, out, err = run_cli(["frobnicate"], root)
    assert code != 0
    assert "Traceback" not in err
    assert "Traceback" not in out


def test_status_on_empty_library_succeeds(root):
    code, out, err = run_cli(["status"], root)
    assert code == 0
    assert "0" in out


def test_board_on_empty_library_writes_file(root):
    code, out, err = run_cli(["board"], root)
    assert code == 0
    assert (root / "board.html").is_file()


def test_sync_from_local_file_then_status_and_board(root):
    index = write_index(root / "in.json", [item(), item("src:bbb", "另一部法规草案")])
    code, out, err = run_cli(["sync", "--from", str(index)], root)
    assert code == 0
    assert "新增 2" in out
    assert (root / "board.html").is_file()

    code, out, err = run_cli(["status"], root)
    assert code == 0
    assert "条目总数 2" in out

    code, out, err = run_cli(["board"], root)
    assert code == 0
    assert "共 2 条" in out


def test_empty_index_is_success(root):
    index = write_index(root / "in.json", [])
    code, out, err = run_cli(["sync", "--from", str(index)], root)
    assert code == 0


def test_broken_json_index_fails_without_dirty_library(root):
    index = root / "in.json"
    index.write_text('{"schema": 2, "items": [', encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(index)], root)
    assert code == 1
    assert not (root / "data" / "items.jsonl").exists()
    assert not (root / "data" / "runs.jsonl").exists()


def test_from_with_neither_file_nor_url(root):
    missing = str(root / "没有这个文件.json")
    code, out, err = run_cli(["sync", "--from", missing], root)
    assert code == 1
    assert "Traceback" not in err
    assert "是不是漏了 https://" in out


def test_python_m_entry_module_is_shipped():
    assert (ROOT / "src" / "legisradar" / "__main__.py").is_file()
    assert (ROOT / "src" / "legisradar" / "__init__.py").is_file()


def test_bad_argument_gives_a_sentence_not_a_stack(root):
    code, out, err = run_cli(["sync", "--nosuchflag"], root)
    assert code != 0
    assert "Traceback" not in err
    assert "Traceback" not in out
    assert "error" in err or "usage" in err


def test_index_json_shaped_envelope_is_accepted(root):
    blob = json.dumps({"schema": 2, "items": []})
    path = root / "in.json"
    path.write_text(blob, encoding="utf-8")
    code, out, err = run_cli(["sync", "--from", str(path)], root)
    assert code == 0


def test_sync_output_lines_are_all_gbk_encodable(root):
    """控制台符号只用 GBK 里有的：重定向时一个不在 GBK 里的字符就会中断整条命令。"""
    index = write_index(root / "in.json", [item()])
    code, out, err = run_cli(["sync", "--from", str(index)], root)
    assert code == 0
    out.encode("gbk")  # 编不过就抛 UnicodeEncodeError


def test_status_output_is_gbk_encodable(root):
    index = write_index(root / "in.json", [item(deadline="2026-10-17")])
    run_cli(["sync", "--from", str(index)], root)
    code, out, err = run_cli(["status"], root)
    assert code == 0
    out.encode("gbk")


def test_all_four_commands_run_in_a_fresh_directory(bare_root, monkeypatch):
    """全新解压、连 data/ 都没有的目录里，四个命令都得能跑起来。"""
    from legisradar import serve

    assert run_cli(["status"], bare_root)[0] == 0
    assert run_cli(["board"], bare_root)[0] == 0

    code, out, err = run_cli(["sync"], bare_root)
    assert code == 1  # 没有索引地址；但它给的是人话，不是栈
    assert "Traceback" not in err
    assert "index_url" in out

    # serve 本身是阻塞的，这里只验它起得来、话说的对。
    # 端口用 0 让系统现挑一个：测试不该依赖某个固定端口在这台机器上是空的。
    monkeypatch.setattr(serve, "serve_forever", lambda *args, **kwargs: None)
    code, out, err = run_cli(["serve", "--port", "0", "--no-open"], bare_root)
    assert code == 0
    assert "本地服务已启动" in out


def test_verbose_flag_is_accepted(root):
    code, out, err = run_cli(["-v", "status"], root)
    assert code == 0
    code, out, err = run_cli(["--verbose", "status"], root)
    assert code == 0
