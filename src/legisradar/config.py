"""配置与程序根。

程序根是启动脚本所在的目录；设了环境变量 LEGISRADAR_ROOT 时以它为准
（把数据放到同步盘之类的场景）。配置文件固定在 <程序根>/config.toml，
data_dir 与 board 给出相对路径时也相对程序根解析。

配置读不懂时整份忽略、全部退回默认值，原因存进模块级的 CONFIG_ERROR。
写库的命令据此拒绝运行：否则 data_dir 会悄悄回落到默认值，程序在程序根下
新建一个空库，而用户真正的数据躺在旁边另一个目录里 —— 看起来像数据全丢了。
只读命令（board / status）用默认值硬跑。
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("legisradar")

CONFIG_NAME = "config.toml"
ROOT_ENV = "LEGISRADAR_ROOT"

DEFAULT_DATA_DIR = "data"
DEFAULT_BOARD = "board.html"
DEFAULT_PORT = 8765

# 最近一次读取配置留下的原因；None 表示配置正常。
CONFIG_ERROR: str | None = None


@dataclass(frozen=True)
class Config:
    """一次运行用的全部配置。

    root / data_dir / board 都是解出来的绝对路径，读写文件一律用它们。
    data_dir_raw 是配置里写下的那个字符串（没写就是默认值），只供显示：
    看板会被整份转发出去，界面上得是用户自己写的那个值。
    """

    root: Path
    data_dir: Path
    data_dir_raw: str
    board: Path
    port: int
    index_url: str
    index_mirrors: tuple[str, ...]
    error: str | None = None

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME


def program_root() -> Path:
    """程序根：环境变量优先，否则是启动脚本所在的那个目录。"""
    override = os.environ.get(ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # 本文件在 <程序根>/src/legisradar/config.py。
    return Path(__file__).resolve().parents[2]


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _table(raw: dict, key: str) -> dict:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _str_list(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out = []
    for entry in value:
        text = _text(entry)
        if text:
            out.append(text)
    return tuple(out)


def _int(value, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base / path


def load(root: Path | str | None = None) -> Config:
    """读配置。读不懂时整份忽略、返回全默认值，并把原因记进 CONFIG_ERROR。"""
    global CONFIG_ERROR

    base = Path(root) if root is not None else program_root()
    path = base / CONFIG_NAME
    raw: dict = {}
    error: str | None = None

    if path.exists():
        try:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("顶层不是一个 TOML 表")
            raw = loaded
        except tomllib.TOMLDecodeError as exc:
            error = (
                f"读不懂 {CONFIG_NAME}：{exc}。"
                "TOML 的双引号字符串里 \\ 是转义符，而 Windows 路径全是反斜杠："
                "路径要写成单引号字面量，例如 'C:\\Users\\你的用户名'"
            )
        except OSError as exc:
            error = f"读不开 {CONFIG_NAME}：{exc}"
        except ValueError as exc:
            error = f"读不懂 {CONFIG_NAME}：{exc}"

    general = _table(raw, "general")
    serve = _table(raw, "serve")
    viewer = _table(raw, "viewer")

    data_value = _text(general.get("data_dir")) or DEFAULT_DATA_DIR
    board_value = _text(general.get("board")) or DEFAULT_BOARD

    CONFIG_ERROR = error
    if error:
        log.warning("%s，%s 被整个忽略，全部用默认配置。", error, CONFIG_NAME)

    return Config(
        root=base,
        data_dir=_resolve(base, data_value),
        data_dir_raw=data_value,
        board=_resolve(base, board_value),
        port=_int(serve.get("port"), DEFAULT_PORT),
        index_url=_text(viewer.get("index_url")),
        index_mirrors=_str_list(viewer.get("index_mirrors")),
        error=error,
    )
