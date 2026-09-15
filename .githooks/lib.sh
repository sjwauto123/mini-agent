# 门禁共用函数。由 .githooks/pre-commit 与 .githooks/commit-msg 各自 source。
# 不要直接执行本文件。

# 解析 python 解释器：优先项目 .venv（只有它装了 playwright）
resolve_py() {
  for candidate in ".venv/Scripts/python.exe" ".venv/bin/python" "python3" "python"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

staged_files() {
  git diff --cached --name-only --diff-filter=ACMRT
}

deleted_files() {
  git diff --cached --name-only --diff-filter=D
}

# 把换行分隔的路径列表缩进打印
list_paths() {
  printf '%s\n' "$1" | grep '^.' | sed 's/^/    /'
}

gate_blocked_footer() {
  echo ""
  echo "提交已被门禁拦下。规则见 AGENTS.md；每条的逃生阀都已在上面的提示里给出。"
}
