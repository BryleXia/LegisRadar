"""分发树本身的字节 —— 不是程序逻辑，是「下载下来能不能用」。

这个仓库最主要的获取方式不是 `git clone`，是点 GitHub 的 **Download ZIP**。
走那条路的人拿到的就是仓库里那份原始字节，没有任何转换环节兜底。
所以这些字节必须在这里钉死，而不是指望谁的编辑器默认值对。

两件已经踩过的事：

* **`.bat` 的编码。** `cmd.exe` 按 OEM 代码页（简体中文是 936）读批处理。
  存成 UTF-8 时，`rem` 里的中文会变成乱码 —— 更糟的是乱码里可能正好
  有 `&` 或 `|`，那一行就会被当成命令分隔符执行。所以 `启动.bat`
  是 **GB18030 + CRLF**，而且 `.gitattributes` 里给它的是 `-text`
  （一个字节都不许动），不是 `text eol=crlf` —— 后者仓库里存的是 LF。
* **`.sh` 的行尾。** CRLF 的 shell 脚本在 Linux 上报的是
  `bash\r: No such file or directory`，看起来像「没装 bash」。

`.bat` 那几条要在**所有**平台上跑，不能跟 `test_launcher` 一样只在
Windows 上跑 —— 否则 CI 在 Linux 上时，恰恰没人看着这份文件。
"""

from __future__ import annotations

import codecs
import pathlib

import pytest

from conftest import ROOT

BAT = ROOT / "启动.bat"
SH = ROOT / "start.sh"

# 只查这些后缀。运行产物（data/、board.html）不进来 —— 它们由程序写，
# 不是每次 checkout 都一样的。
TEXT_SUFFIXES = {".py", ".md", ".toml", ".html", ".json", ".sh", ".txt", ".cfg", ".ini", ".yml", ".yaml"}
SKIP_DIRS = {".git", "data", "__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_FILES = {"board.html"}


def _bare_lf(raw: bytes) -> int:
    return raw.count(b"\n") - raw.count(b"\r\n")


class Test启动bat:

    def test_能在中文windows上被cmd读对(self):
        """解不开就说明根本活不到「双击之后」，后面的测试也没意义。"""
        text = BAT.read_bytes().decode("gb18030")  # 解不开会抛，正好是断言
        assert "立法雷达" in text, "按 GB18030 解出来是乱码 —— 多半被存成 UTF-8 了"
        assert "python" in text.lower()

    def test_每一行都是CRLF(self):
        raw = BAT.read_bytes()
        assert raw.count(b"\r\n") > 50, "CRLF 少得不像话"
        assert _bare_lf(raw) == 0, f"有 {_bare_lf(raw)} 行是裸 LF"

    def test_没有BOM(self):
        """BOM 会让 cmd.exe 把第一行当成一条不存在的命令。"""
        assert not BAT.read_bytes().startswith(codecs.BOM_UTF8)


class Teststartsh:

    def test_是UTF8(self):
        text = SH.read_bytes().decode("utf-8")  # 解不开会抛
        assert text.startswith("#!/usr/bin/env bash")

    def test_没有CR(self):
        """带 CR 的 shebang 在 Linux 上会去找一个叫 `/usr/bin/env\\r` 的程序。"""
        assert b"\r" not in SH.read_bytes()


class Test分发树:

    def test_除bat之外都是LF(self):
        """在 Windows 上用编辑器改一下 `.py`，很容易整份变成 CRLF。
        `.gitattributes` 会在提交时纠正，但**工作区**这一刻已经漂了，
        而下一个命令读的是工作区。"""
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if SKIP_DIRS & set(path.relative_to(ROOT).parts):
                continue
            if path.name in SKIP_FILES or path.suffix == ".bat":
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if _bare_lf(path.read_bytes()) != path.read_bytes().count(b"\n"):
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, "这些文件混进了 CRLF：" + "、".join(offenders)

    @pytest.mark.parametrize("path", [BAT, SH])
    def test_两个启动器都在(self, path):
        """名字里带中文的那份曾经被重命名过，脚本里却还指着旧名字。"""
        assert path.is_file(), f"{path.name} 不见了"
