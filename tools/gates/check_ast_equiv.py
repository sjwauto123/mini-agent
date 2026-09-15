"""结构等价校验：证明一次改动**没有改变代码语义**。

为什么要它
----------
「只加注释」「只是折行」「只是把模块拆成包」这三类任务，看 diff 是看不出
"顺手改了逻辑"的——但 AST 看得出。历史上正是这三类任务出的事故：
补注释时丢掉 `runtime.py` 尾部 300 行、`SYSTEM_PROMPT` 多出前导空格。
所以它们需要的不是"仔细一点"，而是一道**机械门禁**。

比对口径是"折叠空白"级别，不是"任意重排"：
对 .py 文件剥离 COMMENT / docstring / NL / NEWLINE / INDENT / DEDENT 后逐 token 比对。
- 折行、压行、调整缩进 → 等价（token 序列不变）；
- 加注释、改注释、加 docstring → 等价；
- 改一个字符、调换语句顺序、删一行 → **不等价**。

用法
----
    # 比对暂存区 vs HEAD（提交前门禁用这个）
    python tools/gates/check_ast_equiv.py --staged

    # 比对工作区 vs HEAD
    python tools/gates/check_ast_equiv.py

    # 指定文件
    python tools/gates/check_ast_equiv.py --staged backend/mini_agent/runtime.py

不加参数时自动取本次改动涉及的 `.py` 文件。退出码 1 表示存在不等价文件。
"""
from __future__ import annotations

import ast
import io
import subprocess
import sys
import tokenize
from pathlib import Path

IGNORED_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)

DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def strip_to_code(source: str) -> str:
    """剥离注释与 docstring，返回剩余的代码 token 序列（以 \\x00 连接）。"""
    tree = ast.parse(source)
    doc_positions = set()
    for node in ast.walk(tree):
        if isinstance(node, DOCSTRING_OWNERS):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc_positions.add((body[0].lineno, body[0].col_offset))

    pieces = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in IGNORED_TOKENS:
            continue
        if token.type == tokenize.STRING and (token.start[0], token.start[1]) in doc_positions:
            continue
        pieces.append(token.string)
    return "\x00".join(pieces)


def read(spec: str) -> str:
    """读取 `git show` 能解析的任意版本规格（如 HEAD:path、:path）。"""
    result = subprocess.run(
        ["git", "show", spec], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout if result.returncode == 0 else ""


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def changed_python_files(staged: bool) -> list[str]:
    args = ["diff", "--cached"] if staged else ["diff"]
    args += ["--name-only", "--diff-filter=d", "--", "*.py"]
    return [line for line in git(*args).splitlines() if line.strip()]


def numstat(path: str, staged: bool) -> tuple[str, str]:
    args = ["diff", "--cached"] if staged else ["diff"]
    args += ["--numstat", "--", path]
    line = git(*args).strip()
    if not line:
        return "?", "?"
    added, removed, *_ = line.split("\t")
    return added, removed


def check(path: str, staged: bool) -> tuple[bool, str]:
    """返回 (是否等价, 报告行)。"""
    new = read(f":{path}") if staged else Path(path).read_text(encoding="utf-8")
    old = read(f"HEAD:{path}")
    added, removed = numstat(path, staged)

    if not old:
        return True, f"[新文件] {path}  +{added}/-{removed}（HEAD 中不存在，跳过等价校验）"
    if new == "":
        return True, f"[已删除] {path}  +{added}/-{removed}（按删除处理，不在此门禁范围内）"
    try:
        same = strip_to_code(old) == strip_to_code(new)
    except SyntaxError as exc:
        return False, f"[语法错] {path}  {exc}"
    flag = "等价" if same else "不等价"
    return same, f"[{flag}] {path}  +{added}/-{removed}"


def main(argv: list[str]) -> int:
    staged = "--staged" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        paths = changed_python_files(staged)
    if not paths:
        print("没有需要校验的 .py 改动。")
        return 0

    print(f"结构等价校验（{':（暂存区）' if staged else '工作区'} vs HEAD），共 {len(paths)} 个文件：")
    failures = []
    for path in paths:
        ok, line = check(path, staged)
        print("  " + line)
        if not ok:
            failures.append(path)

    if failures:
        print("\n以下文件**代码结构已改变**，不能按「注释/折行/拆分」提交：")
        for path in failures:
            print(f"  - {path}")
        print("\n两条路：① 用 修复：/功能：/重构： 前缀正常提交；")
        print("          ② 若确认只是格式调整，把改动拆出来单独看 diff。")
        return 1
    print("\n全部等价：这次改动没有改变代码语义。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
