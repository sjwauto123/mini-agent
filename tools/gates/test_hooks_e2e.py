"""钩子的**端到端**测试：在一次性临时仓库里真的执行 `git commit`。

为什么必须有这一层
------------------
单元测试里我是用 `GATE_COMMIT_MSG` 环境变量把提交信息喂给钩子的，于是**全部通过**。
但真实的 `git commit` 流程是：
    1. 跑 pre-commit
    2. 才写提交信息
    3. 跑 commit-msg
所以 pre-commit 阶段读到的 `.git/COMMIT_EDITMSG` 是**上一条**提交的信息。
结果就是门禁 2 会"看情况随机生效"——前一条恰好也是「注释：」时才偶然拦住。
这个 bug 只有真跑 `git commit` 才暴露得出来。

本用例把它固化成断言：**门禁 2 必须依赖本次提交信息**。
如果谁把结构等价校验挪回 pre-commit，这里会失败。

用 tempfile 建临时仓库，跑完即删，不碰本仓库。

用法：python tools/gates/test_hooks_e2e.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CASES: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    CASES.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))


def setup_sandbox() -> Path:
    root = Path(tempfile.mkdtemp(prefix="gate-e2e-"))
    for name in (".githooks", "tools"):
        shutil.copytree(REPO / name, root / name)
    hook = root / ".githooks" / "pre-commit"
    hook.chmod(hook.stat().st_mode | 0o111)
    msg_hook = root / ".githooks" / "commit-msg"
    msg_hook.chmod(msg_hook.stat().st_mode | 0o111)

    def run(*args, **kw):
        return subprocess.run(args, cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **kw)

    run("git", "init", "-q", ".")
    run("git", "config", "core.hooksPath", ".githooks")
    run("git", "config", "user.email", "gate@test")
    run("git", "config", "user.name", "gate")
    run("git", "config", "commit.gpgsign", "false")
    return root


def main() -> int:
    root = setup_sandbox()

    def run(*args, **kw):
        return subprocess.run(args, cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **kw)

    def commit(message: str):
        return run("git", "commit", "-m", message)

    def commit_count() -> int:
        return len(run("git", "log", "--oneline").stdout.strip().splitlines())

    print("钩子端到端测试（临时仓库里真跑 git commit）\n")

    (root / "errors.py").write_text('KNOWN = frozenset({"a"})\n', encoding="utf-8")
    run("git", "add", "errors.py")
    baseline = commit("初始化：建立基线")
    record("基线提交成功", baseline.returncode == 0)

    # --- 关键回归：注释前缀 + 语义改动，必须被拦
    (root / "errors.py").write_text('KNOWN = frozenset({"b"})\n', encoding="utf-8")
    run("git", "add", "errors.py")
    blocked = commit("注释：补充常量注释")
    detail = "".join(blocked.stdout + blocked.stderr)
    record(
        "「注释：」+ 语义改动 → 真实 git commit 被拦（回归：曾因 pre-commit 读到旧信息而放行）",
        blocked.returncode != 0 and "不等价" in detail,
        f"退出码={blocked.returncode}",
    )
    record("被拦后没有产生提交", commit_count() == 1, f"提交数={commit_count()}")

    # --- 前缀正确时不该被误拦
    ok_commit = commit("修复：修正常量取值")
    record("改用「修复：」前缀 → 正常提交", ok_commit.returncode == 0, f"提交数={commit_count()}")

    # --- 注释前缀 + 纯注释改动，应该放行
    source = (root / "errors.py").read_text(encoding="utf-8")
    (root / "errors.py").write_text("# 只是加一行注释\n" + source, encoding="utf-8")
    run("git", "add", "errors.py")
    comment_commit = commit("注释：补充常量注释")
    record("「注释：」+ 纯注释改动 → 放行", comment_commit.returncode == 0, f"提交数={commit_count()}")

    # --- 门禁 3 在真实提交里生效
    (root / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "src" / "App.tsx").write_text("export const a = 1;\n", encoding="utf-8")
    run("git", "add", "frontend/src/App.tsx")
    frontend_blocked = commit("前端：调整气泡样式")
    detail = "".join(frontend_blocked.stdout + frontend_blocked.stderr)
    record(
        "触及展示层 → 真实 git commit 被拦",
        frontend_blocked.returncode != 0 and "必须人工看一眼真机表现" in detail,
        f"退出码={frontend_blocked.returncode}",
    )

    # --- 逃生阀在真实提交里生效
    env_reviewed = {**os.environ, "GATE_REVIEWED": "1"}
    frontend_ok = subprocess.run(
        ["git", "commit", "-m", "前端：调整气泡样式"], cwd=root, capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env_reviewed,
    )
    record("GATE_REVIEWED=1 → 真实 git commit 放行", frontend_ok.returncode == 0, f"提交数={commit_count()}")

    # --- 门禁 1：一次删 3 个文件
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")
    run("git", "add", "a.py", "b.py", "c.py")
    run("git", "commit", "-m", "功能：新增三个模块")
    for name in ("a.py", "b.py", "c.py"):
        (root / name).unlink()
    run("git", "add", "-A")
    bulk = commit("重构：清理三个模块")
    detail = "".join(bulk.stdout + bulk.stderr)
    record(
        "一次删除 3 个跟踪文件 → 真实 git commit 被拦",
        bulk.returncode != 0 and "本次提交要删除 3 个跟踪文件" in detail,
        f"退出码={bulk.returncode}",
    )

    shutil.rmtree(root, ignore_errors=True)

    failed = [c for c in CASES if not c[1]]
    print(f"\n=== 汇总：{len(CASES) - len(failed)}/{len(CASES)} 端到端用例符合预期 ===")
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
