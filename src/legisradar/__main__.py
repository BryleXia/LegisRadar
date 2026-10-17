"""``python -m legisradar`` 的入口。

启动脚本按 ``<程序根>/src`` 找这个包，靠的就是这个模块。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
