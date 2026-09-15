"""跑一遍门禁自身的测试。

为什么门禁要有测试：本项目已经吃过一次亏——`pyproject.toml` 里写了 ruff 配置，
但本机装不上 ruff，于是那份配置**从来没有真正跑过**，属于纸面规则。
门禁比 linter 更强硬（它会真的拦住提交），所以更不能靠"看起来对"。

    python tools/gates/selftest.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SUITES = [
    "test_ast_equiv.py",
    "test_hooks.py",
    "test_hooks_e2e.py",
]


def main() -> int:
    failed = []
    for name in SUITES:
        print(f"\n{'=' * 72}\n{name}\n{'=' * 72}")
        proc = subprocess.run(
            [sys.executable, str(HERE / name)], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        print((proc.stdout or "") + (proc.stderr or ""), end="")
        if proc.returncode != 0:
            failed.append(name)

    print(f"\n{'=' * 72}\n门禁自测汇总\n{'=' * 72}")
    for name in SUITES:
        print(f"  {'FAIL' if name in failed else 'PASS'}  {name}")
    if failed:
        print("\n门禁本身有问题时不要装作没看见：它拦错一次，人就会习惯性加逃生阀绕过它。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
