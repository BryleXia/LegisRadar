"""对比度 —— 文字与它**真正落在的那个底色**之间的比值。

这一组不是审美检查。用户要求「浅色模式黑字全黑、暗色模式白字全白」，
纯黑纯白那两条已经由 `test_board` 钉住；这里补的是剩下那几档语义色：
即将截止的琥珀、告警的砖红、强调的墨绿。

**为什么要算而不是看。** 浅色那份的 `--soon` 原本是 `#A8600C`，
在白底（`--surface`）上是 4.83:1 —— 单看这一对完全合格。但它真正的落点是
`--paper`（`#F1F3F2`）和徽标底 `--soon-s`（`#FAF0E1`），两个更暗的底，
实际只有 4.34 和 4.29。肉眼分不出这零点几，它正好卡在 AA 的线下面。

顺带钉住另一件同样容易出的事：`:root[data-theme="dark"]` 把整套令牌
**手抄了一遍**。抄漏一个不会报错 —— `var(--x)` 找不到就是空，
那条规则整条失效，而且只在暗色下失效。
"""

from __future__ import annotations

import pathlib
import re
from functools import lru_cache

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "legisradar" / "templates" / "board.html"

# 正文的 4.5:1 是 WCAG AA 对普通字号的要求。这几档字号都在 12~13px，
# 够不上「大字号」那条 3:1 的宽松线，所以不给自己留后门。
AA = 4.5

TOKEN = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})")

# 三块 `:root` 的锚点。都是平铺的 `--名: #hex;`，里面没有嵌套花括号，
# 所以用「到下一个收尾花括号」来切是稳的。
_ANCHORS = {
    "light": r"\n:root \{(.*?)\n\}",
    "dark": r'\n:root\[data-theme="dark"\] \{(.*?)\n\}',
}


@lru_cache(maxsize=None)
def blocks() -> dict[str, dict[str, str]]:
    text = TEMPLATE.read_text(encoding="utf-8")
    out = {}
    for name, pattern in _ANCHORS.items():
        found = re.search(pattern, text, re.S)
        assert found, f"模板里找不到这块 CSS（{name}）：{pattern}"
        out[name] = dict(TOKEN.findall(found.group(1)))
    return out


def _channel(value: int) -> float:
    srgb = value / 255
    return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def themes():
    b = blocks()
    return (("浅色", b["light"]), ("暗色", b["dark"]))


class Test两套主题的令牌要一起长:

    def test_浅色和暗色定义同一组令牌(self):
        """暗色那组是**手抄**的一份。抄漏一个不会报错 —— `var(--x)` 找不到
        就是空，那条规则整条失效，而且只在暗色下失效。"""
        b = blocks()
        assert set(b["light"]) == set(b["dark"]), "两套主题的令牌名对不上"


class Test文字要看得清:

    def test_正文与次要文字达到AA(self):
        for label, tokens in themes():
            for fg in ("ink", "ink-2", "muted"):
                for bg in ("paper", "surface"):
                    got = ratio(tokens[fg], tokens[bg])
                    assert got >= AA, (
                        f"{label}：{fg} {tokens[fg]} 落在 {bg} {tokens[bg]} 上只有 "
                        f"{got:.2f}:1（要 {AA}）"
                    )

    def test_语义色也要达到AA(self):
        """强调绿、告警红、即将截止的琥珀 —— 它们都当正文用过：
        组标题、徽标文字、倒计时数字。"""
        for label, tokens in themes():
            for fg in ("accent", "urgent", "soon"):
                for bg in ("paper", "surface", f"{fg}-s"):
                    got = ratio(tokens[fg], tokens[bg])
                    assert got >= AA, (
                        f"{label}：{fg} {tokens[fg]} 落在 {bg} {tokens[bg]} 上只有 "
                        f"{got:.2f}:1（要 {AA}）"
                    )
