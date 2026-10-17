"""十二、文件形状与编码。"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tomllib

import pytest

from conftest import ROOT
from legisradar import board, sync

SKIP_DIRS = {"__pycache__", ".git", "data", ".pytest_cache", "build", "dist"}
BAT_SUFFIXES = {".bat", ".cmd"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip"}


def shipped_files() -> list[pathlib.Path]:
    out = []
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        if any(part.endswith(".egg-info") for part in path.parts):
            continue
        out.append(path)
    return out


def test_bat_files_are_crlf_through_and_through():
    bats = [path for path in shipped_files() if path.suffix.lower() in BAT_SUFFIXES]
    assert bats, "分发树里应当有 Windows 启动器"
    for path in bats:
        raw = path.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n"), f"{path} 里有裸 LF"


def test_startup_bat_decodes_as_gb18030():
    raw = (ROOT / "启动.bat").read_bytes()
    text = raw.decode("gb18030")  # 解不开就抛
    assert "立法雷达" in text
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_executable_lines_of_the_launchers_have_no_inverted_errorlevel_check():
    bat_source = (ROOT / "启动.bat").read_bytes().decode("gb18030")
    for line in bat_source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("rem"):
            continue
        assert "!errorlevel!==" not in stripped.replace(" ", ""), line

    sh_source = (ROOT / "start.sh").read_text(encoding="utf-8")
    for line in sh_source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert "!errorlevel!==" not in stripped, line


def test_every_text_file_is_lf():
    for path in shipped_files():
        if path.suffix.lower() in BAT_SUFFIXES or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        raw = path.read_bytes()
        assert b"\r\n" not in raw, f"{path} 里有 CRLF"


def test_no_text_file_carries_a_bom():
    for path in shipped_files():
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf"), path


def test_no_control_characters_beyond_tab_and_newline():
    for path in shipped_files():
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        raw = path.read_bytes()
        if path.suffix.lower() in BAT_SUFFIXES:
            raw = raw.replace(b"\r\n", b"\n")
        for index, byte in enumerate(raw):
            if byte in (0x09, 0x0A):
                continue
            assert byte >= 0x20, f"{path} 第 {index} 字节是控制字符 {byte:#04x}"
            assert byte != 0x7F, f"{path} 第 {index} 字节是 DEL"


def test_start_sh_has_the_executable_bit():
    """Windows 的 NTFS 不把可执行位暴露给 os.stat，所以这里问 MSYS 的 test。"""
    path = (ROOT / "start.sh").as_posix()
    bash = shutil.which("bash") or next(
        (
            candidate
            for candidate in (
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files (x86)\Git\bin\bash.exe",
            )
            if pathlib.Path(candidate).exists()
        ),
        None,
    )
    if bash:
        probe = subprocess.run(
            [bash, "-c", f'test -x "{path}" && echo YES || echo NO'],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert probe.stdout.strip() == "YES", probe.stderr
        return
    assert os.access(ROOT / "start.sh", os.X_OK)


def test_start_sh_declares_a_bash_shebang():
    first = (ROOT / "start.sh").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("#!")
    assert "bash" in first or "sh" in first


def test_packaging_declares_the_template():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []
    assert data["project"]["requires-python"] >= "3.11"
    package_data = data["tool"]["setuptools"]["package-data"]
    assert "templates/*.html" in package_data["legisradar"]
    assert data["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


def test_template_file_exists_and_is_readable():
    text = board.read_template()
    assert text.strip()
    assert board.DATA_PLACEHOLDER in text
    assert board.TOKEN_META in text
    assert (ROOT / "src" / "legisradar" / "templates" / "board.html").is_file()


def test_module_tree_is_complete():
    package = ROOT / "src" / "legisradar"
    for name in (
        "__init__.py",
        "__main__.py",
        "cli.py",
        "config.py",
        "models.py",
        "store.py",
        "board.py",
        "serve.py",
        "sync.py",
    ):
        assert (package / name).is_file(), name


def test_gitignore_covers_runtime_products():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/data/" in text
    assert "/board.html" in text


def test_bundled_index_is_a_publish_artifact_not_a_build_input():
    """随包索引是发布流程的产物，不是源码的一部分。

    它现在不在仓库里（由发布那一步放进去）；一旦在，它就成了断网时的退路，
    所以那时候它必须是一份程序读得懂、且写着汇总时间的 schema 2 索引。
    """
    path = ROOT / "index.json"
    if not path.exists():
        return
    parsed = sync.parse_index(path.read_bytes())
    assert parsed.generated_at
    assert parsed.items
