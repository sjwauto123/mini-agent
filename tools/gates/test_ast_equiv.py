"""`check_ast_equiv.py` 自测：用临时索引验证「注释等价」与「语义改动」能被正确区分。

三个用例：只加注释 → 等价；改字符串字面量 → 不等价；折行/空白调整 → 等价。

不碰工作区、不碰真实暂存区：GIT_INDEX_FILE 指向临时文件，blob 用 git hash-object 造。

用法：python tools/gates/test_ast_equiv.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TARGET = "backend/mini_agent/errors.py"
TMP_INDEX = Path(tempfile.mkdtemp(prefix="gate-ast-")) / "index"


def run(args, **kw):
    env = {**os.environ, "GIT_INDEX_FILE": str(TMP_INDEX)}
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, encoding="utf-8", env=env, **kw)


def head_source(path: str) -> str:
    return run(["git", "show", f"HEAD:{path}"]).stdout


def stage_source(source: str) -> None:
    blob = run(["git", "hash-object", "-w", "--stdin"], input=source).stdout.strip()
    assert blob, "hash-object 失败"
    result = run(["git", "update-index", "--cacheinfo", f"100644,{blob},{TARGET}"])
    assert result.returncode == 0, f"update-index 失败: {result.stderr}"


def main() -> int:
    if TMP_INDEX.exists():
        TMP_INDEX.unlink()
    assert run(["git", "read-tree", "HEAD"]).returncode == 0, "read-tree 失败"

    original = head_source(TARGET)
    assert original, f"取不到 {TARGET}"
    lines = original.split("\n")

    results = []

    # ---- 用例 1：只插入一行注释，代码结构不变 → 应判"等价"，退出码 0
    commented = lines[:]
    commented.insert(len(commented) // 2, "# 门禁自测：这行只是注释")
    stage_source("\n".join(commented))
    proc = run([sys.executable, str(REPO / "tools" / "gates" / "check_ast_equiv.py"), "--staged"])
    results.append(("只加注释 → 等价", proc.returncode == 0, proc.stdout.strip().splitlines()[0:2]))

    # ---- 用例 2：改一个字符串字面量（语义变了）→ 应判"不等价"，退出码 1
    changed = original.replace('"session_not_found"', '"session_missing"', 1)
    assert changed != original, "替换没生效，用例无效"
    stage_source(changed)
    proc = run([sys.executable, str(REPO / "tools" / "gates" / "check_ast_equiv.py"), "--staged"])
    results.append(("改字符串字面量 → 不等价", proc.returncode == 1, proc.stdout.strip().splitlines()[-4:]))

    # ---- 用例 3：整行折行（把一条长语句拆成两行）→ 应判"等价"
    wrapped = original.replace(
        "KNOWN_CODES = frozenset(ERROR_ANSWERS)",
        "KNOWN_CODES = frozenset(\n    ERROR_ANSWERS\n)",
        1,
    )
    if wrapped == original:
        # 该常量名不存在时，退而用"在文件末尾插入空行"作为等价用例
        wrapped = original.rstrip("\n") + "\n\n\n"
    stage_source(wrapped)
    proc = run([sys.executable, str(REPO / "tools" / "gates" / "check_ast_equiv.py"), "--staged"])
    results.append(("折行/空白调整 → 等价", proc.returncode == 0, proc.stdout.strip().splitlines()[0:2]))

    TMP_INDEX.unlink(missing_ok=True)

    print("\n=== 自测结果 ===")
    failed = 0
    for name, ok, sample in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
            for line in sample:
                print(f"        {line}")
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
