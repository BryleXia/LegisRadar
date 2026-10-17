#!/usr/bin/env bash
# 立法雷达（查看器）· macOS / Linux 启动器
#
# 这里只认系统 python3，找不到就明确报错。
# 不做任何未经同意的系统改动；没装 Python 时也只给指引，不替你装。
#
# 这个程序零依赖，只要能跑 Python 3.11+ 就行 —— 不需要 pip 装任何库。

set -euo pipefail
cd "$(dirname "$0")"

echo
echo "  立法雷达 LegisRadar"
echo "  看看有哪些法规草案正在征求意见"
echo "  --------------------------------------------------"

if ! command -v python3 >/dev/null 2>&1; then
  cat <<'EOF'

  ✗ 没找到 python3。

  到 python.org 装一个 Python 3.11 或更新版：
      https://www.python.org/downloads/
  或者用系统自带的包管理器，例如
      macOS:  brew install python@3.12
      Ubuntu: sudo apt install python3.12

  本脚本只做检测，不会替你下载或安装任何东西。

EOF
  exit 1
fi

if ! python3 -c '' 2>/dev/null; then
  # ★ `command -v python3` 成功不等于 python3 能用。
  #
  # macOS 自带的 /usr/bin/python3 是 Xcode 命令行工具的壳 —— 它存在、
  # 命令找得到，但跑起来只会弹一个「要装命令行工具」的图形窗（或者直接
  # 非零退出）。把这种情况说成「你的 python3 版本太低」是错的：那台机器上
  # 压根没装过 Python，而「升级」和「安装」是两件不同的事。
  cat <<'EOF'

  ✗ 找到了 python3，但它跑不起来。

  macOS 上这多半是：/usr/bin/python3 只是 Xcode 命令行工具的壳，还没装真的
  Python。装一个再回来：
      xcode-select --install        （先装命令行工具）
      brew install python@3.12      （再装 Python）
  或者直接到 python.org 下载安装包。

  本脚本只做检测，不会替你下载或安装任何东西。

EOF
  exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
  cat <<'EOF'

  ✗ 这台电脑上的 python3 版本低于 3.11。

  升级到 3.11 或更新版 —— 不必先卸掉旧的，把新的装上去覆盖就行：
      macOS:  brew upgrade python
      Ubuntu: sudo apt install python3.12

  为什么非要 3.11：本程序读配置用 tomllib，它到 3.11 才进标准库。
  更早的版本上读不了 config.toml，程序只能靠默认值硬跑。

EOF
  exit 1
fi

echo "  [环境] 使用系统 Python3"

export PYTHONPATH="$PWD/src"
export PYTHONIOENCODING=utf-8

echo "  [执行] 拉取最新条目…"
echo

# ★ 显式接住退出码，不靠 set -e 了事。
#
# sync 的退出码是设计的一部分：离线但本地有数据时它返回 0（并且顺手重出了
# 一版看板）；返回 1 的是离线且本地空、没有索引地址、索引一条都用不了这类
# 情形。让 set -e 在返回 1 时直接掐掉脚本，等于扔掉 sync.py 专门为离线写的
# 降级：本地明明有上一次的数据，却连看板都看不到。所以这里只提示，接着往下
# 跑 board 和 serve。
rc=0
python3 -m legisradar sync "$@" || rc=$?
echo

if [ "$rc" -ne 0 ]; then
  echo "  ⚠ 这次没同步上（退出码 $rc）。"
  echo "     多半是网络不通，或者索引地址一时打不开。"
  echo "     看板照样出，但上面的日期可能停在上一回。"
  echo
fi

echo "  [执行] 生成看板…"
python3 -m legisradar board
echo

if [ -f board.html ]; then
  # 起本地服务而不是直接 open —— 「已截止」按钮需要它。
  # 静态页写不了文件，点得动却改不了的假按钮比没有更糟。
  echo "  [执行] 起本地服务：页面上可手动标「已截止」…"
  python3 -m legisradar serve
else
  echo "  看板未生成，请检查上面的日志。"
fi
