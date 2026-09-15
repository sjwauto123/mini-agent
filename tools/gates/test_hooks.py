"""`.githooks/` 两个钩子的分支测试（单元级，用临时索引构造暂存状态）。

覆盖：
  pre-commit  的门禁 1（批量删除）、门禁 3（人工过目）
  commit-msg  的门禁 2（结构等价）、门禁 2b（注释类只增不删）

调用方式刻意贴近真实：commit-msg 通过**把信息写进临时文件、再作为 `$1` 传入**来测，
而不是走环境变量——因为 `$1` 才是 git 实际传参的方式，也是这次 bug 的所在。

不碰工作区、不碰真实暂存区、不产生任何提交。

用法：python tools/gates/test_hooks.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".githooks"
TMP = Path(tempfile.mkdtemp(prefix="gate-hooks-"))
TMP_INDEX = TMP / "index"
MSG_FILE = TMP / "COMMIT_MSG"

PY_TARGET = "backend/mini_agent/errors.py"
FRONTEND_TARGET = "frontend/src/App.tsx"
MD_TARGET = "docs/design.md"
DELETE_TARGETS = [
    "backend/mini_agent/config.py",
    "backend/mini_agent/contracts.py",
    "backend/mini_agent/errors.py",
]


def git(args, *, stdin=None):
    """走字节模式：Windows 上 text 模式会把写入子进程的 \\n 翻译成 \\r\\n，
    于是"只插一行注释"会被暂存成全文件行尾改写（+93/-92），把用例本身弄脏。"""
    env = {**os.environ, "GIT_INDEX_FILE": str(TMP_INDEX)}
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, env=env, input=data)
    proc.out = proc.stdout.decode("utf-8", errors="replace")
    proc.err = proc.stderr.decode("utf-8", errors="replace")
    return proc


def reset_index():
    TMP_INDEX.unlink(missing_ok=True)
    result = git(["read-tree", "HEAD"])
    assert result.returncode == 0, f"read-tree 失败: {result.err}"


def head_source(path):
    return git(["show", f"HEAD:{path}"]).out


def stage_content(path, content):
    blob = git(["hash-object", "-w", "--stdin"], stdin=content).out.strip()
    assert blob, f"hash-object 失败: {path}"
    result = git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"])
    assert result.returncode == 0, f"update-index 失败: {result.err}"


def stage_delete(path):
    result = git(["update-index", "--force-remove", path])
    assert result.returncode == 0, f"force-remove 失败: {result.err}"


def run_hook(hook, message, **env_overrides):
    """按 git 的真实方式调用：commit-msg 收 $1 = 提交信息文件路径。"""
    MSG_FILE.write_text(message + "\n", encoding="utf-8")
    env = {**os.environ, "GIT_INDEX_FILE": str(TMP_INDEX)}
    for key in ("GATE_ALLOW_BULK_DELETE", "GATE_SKIP_AST_EQUIV", "GATE_REVIEWED", "GATE_COMMIT_MSG"):
        env.pop(key, None)
    env.update(env_overrides)
    proc = subprocess.run(
        ["sh", str(HOOKS / hook), str(MSG_FILE)], cwd=REPO, capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


CASES = []


def case(name, hook, message, *, stage, expect_code, expect_text, env_overrides=None):
    reset_index()
    stage()
    code, out = run_hook(hook, message, **(env_overrides or {}))
    ok = (code == expect_code) and (expect_text in out)
    CASES.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({hook}，退出码 {code}，期望 {expect_code})")
    if not ok:
        if expect_text not in out:
            print(f"        未出现期望文案：{expect_text}")
        for line in out.strip().splitlines()[-12:]:
            print(f"        | {line}")


def main() -> int:
    original_py = head_source(PY_TARGET)
    original_tsx = head_source(FRONTEND_TARGET)
    original_md = head_source(MD_TARGET)

    comment_only = "# 门禁自测：这行是注释，不改变结构\n" + original_py
    semantic = original_py.replace('"session_not_found"', '"session_missing"', 1)
    assert semantic != original_py, "语义改动用例没生效"

    print("钩子分支自测（每个分支断言退出码 + 专属文案）\n")

    # ---- pre-commit：门禁 3 ----
    case(
        "触及前端展示层 → 门禁 3 阻断",
        "pre-commit", "前端：调整气泡样式",
        stage=lambda: stage_content(FRONTEND_TARGET, original_tsx + "\n// 自测\n"),
        expect_code=1, expect_text="必须人工看一眼真机表现",
    )
    case(
        "已确认过目（GATE_REVIEWED=1）→ 放行",
        "pre-commit", "前端：调整气泡样式",
        stage=lambda: stage_content(FRONTEND_TARGET, original_tsx + "\n// 自测\n"),
        expect_code=0, expect_text="已确认人工过目",
        env_overrides={"GATE_REVIEWED": "1"},
    )

    # ---- pre-commit：门禁 1 ----
    def stage_three_deletes():
        for path in DELETE_TARGETS:
            stage_delete(path)

    case(
        "一次删除 3 个跟踪文件 → 门禁 1 阻断",
        "pre-commit", "重构：清理无用模块",
        stage=stage_three_deletes,
        expect_code=1, expect_text="本次提交要删除 3 个跟踪文件",
    )
    case(
        "批量删除已确认（GATE_ALLOW_BULK_DELETE=1）→ 放行",
        "pre-commit", "重构：清理无用模块",
        stage=stage_three_deletes,
        expect_code=0, expect_text="已被 GATE_ALLOW_BULK_DELETE=1 放行",
        env_overrides={"GATE_ALLOW_BULK_DELETE": "1"},
    )

    # ---- commit-msg：门禁 2 / 2b ----
    case(
        "普通前缀 + 语义改动 → commit-msg 放行（不适用）",
        "commit-msg", "修复：修正错误码",
        stage=lambda: stage_content(PY_TARGET, semantic),
        expect_code=0, expect_text="",
    )
    case(
        "「注释」前缀 + 纯注释改动 → 放行",
        "commit-msg", "注释：补充错误码注释",
        stage=lambda: stage_content(PY_TARGET, comment_only),
        expect_code=0, expect_text="提交信息门禁通过",
    )
    case(
        "「注释」前缀 + 语义改动 → 门禁 2 阻断",
        "commit-msg", "注释：补充错误码注释",
        stage=lambda: stage_content(PY_TARGET, semantic),
        expect_code=1, expect_text="不等价",
    )
    case(
        "「工程」前缀 + 语义改动 → 同样被门禁 2 拦（前缀无法绕过）",
        "commit-msg", "工程：调整格式",
        stage=lambda: stage_content(PY_TARGET, semantic),
        expect_code=1, expect_text="不等价",
    )
    case(
        "「注释」前缀 + 非 py 文件删行 → 门禁 2b 阻断",
        "commit-msg", "注释：给设计文档补注释",
        stage=lambda: stage_content(MD_TARGET, "\n".join(original_md.split("\n")[1:])),
        expect_code=1, expect_text="不允许删除任何行",
    )
    case(
        "门禁 2 已跳过（GATE_SKIP_AST_EQUIV=1）→ 放行",
        "commit-msg", "注释：补充错误码注释",
        stage=lambda: stage_content(PY_TARGET, semantic),
        expect_code=0, expect_text="已被 GATE_SKIP_AST_EQUIV=1 跳过",
        env_overrides={"GATE_SKIP_AST_EQUIV": "1"},
    )

    TMP_INDEX.unlink(missing_ok=True)

    failed = [name for name, ok in CASES if not ok]
    print(f"\n=== 汇总：{len(CASES) - len(failed)}/{len(CASES)} 分支符合预期 ===")
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
