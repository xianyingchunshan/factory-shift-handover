"""``python -m skill`` 等价入口（与 ``python -m skill.run`` 同一实现）。"""

from __future__ import annotations

import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main())
