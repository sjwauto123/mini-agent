"""QA 统一入口：跑验收脚本，并把结果落成结构化证据到 `tools/qa/evidence/*.json`。

为什么要这个东西
----------------
此前这些脚本只把结果打到 stdout，被重定向成 `.pytest-tmp/*.log`——而 `.pytest-tmp/` 被
`.gitignore` 覆盖，于是"验过了"这件事**只存在于本机**：评审拿不到、CI 拿不到、换个时间
自己也复跑不出同样的结论。把脚本移进仓库只解决了一半，另一半是**让每次运行都留下可提交、
可 diff、可追溯的机器可读结果**。

设计要点
--------
1. 证据绑定三样东西：**脚本内容哈希** + **git 提交号** + **服务与模型的实测状态**。
   没有这三样，"通过了"这句话无法定位到任何一个可复现的状态。
2. 前置条件机器可检：先探 `/api/health` 和 `/api/models`，不具备就**立刻失败并说清原因**，
   而不是让脚本跑一半报一堆看不懂的错。
3. 退出码即结论：有任何 FAIL 就非 0，供 `&&` 串接或 CI 直接消费。

用法
----
    python tools/qa/run.py list
    python tools/qa/run.py all                    # 除 boundary 外的全部
    python tools/qa/run.py all --with-boundary    # 连外置资源边界一起跑
    python tools/qa/run.py smoke                  # 跑单个
    python tools/qa/run.py --python .venv/Scripts/python.exe smoke
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
EVIDENCE_DIR = HERE / "evidence"
DEFAULT_BASE = "http://127.0.0.1:8000"

# 名称：(脚本, 说明, 需要真实模型, 需要浏览器)
# 说明里不写检查项数量：数量随断言增减而变化，写死会立刻变成一句不实的陈述。
SUITES: dict[str, tuple[str, str, bool, bool]] = {
    "behavior": ("qa_behavior.py", "真实模型 · 行为检查：回答规范性 / 权限边界 / 前端载荷 / 链路", True, False),
    "browser": ("qa_browser_render.py", "真实模型 + Chrome · 渲染检查：回答气泡与执行日志", True, True),
    "matrix": ("qa_matrix.py", "真实模型 · 上下文加固回归（工具链/记忆/注入/边界）", True, False),
    "trace-fields": ("trace_fields_browser.py", "不调模型 · 历史数据渲染检查（自发现 fixture，缺数据报 SKIP）", False, True),
    "trace-live": ("trace_live_refresh.py", "真实模型 + Chrome · 运行期间执行日志刷新", True, True),
    "smoke": ("final_smoke.py", "真实模型 · 快速冒烟：回答正确 / 真流式 / 工具增量", True, False),
    "boundary": ("qa_boundary.py", "真实模型 · 外置资源边界（发 ~2.8M 字）", True, False),
}

# `all` 默认不包含：单次运行要发 2.8M 字，代价与上面几套不在一个量级
HEAVY = {"boundary"}

CHECK_RE = re.compile(r"^\s*\[(PASS|FAIL|ADVISORY|SKIP)\]\s*(.*)$")
RESULT_RE = re.compile(r"^RESULT:\s*(.+?)\s*$")


def pick_python(explicit: str | None) -> str:
    """优先项目 .venv（只有它装了 playwright），其次当前解释器。"""
    if explicit:
        return explicit
    for rel in ("Scripts/python.exe", "bin/python"):
        candidate = REPO_ROOT / ".venv" / rel
        if candidate.exists():
            return str(candidate)
    return sys.executable


def git_rev() -> tuple[str, bool]:
    def run(args: list[str]) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()

    rev = run(["rev-parse", "--short", "HEAD"]) or "(no git)"
    dirty = bool(run(["status", "--porcelain"]))
    return rev, dirty


def probe_service(base: str, timeout: float = 5.0) -> dict:
    """直连本机，绕开系统代理（本机代理会把 127.0.0.1 变成 502）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    out: dict = {"base": base, "healthy": False, "models": []}
    try:
        with opener.open(f"{base}/api/health", timeout=timeout) as resp:
            out["health"] = json.loads(resp.read().decode() or "{}")
            out["healthy"] = True
    except Exception as exc:  # noqa: BLE001 - 前置检查失败原因要原样带出来
        out["health_error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        with opener.open(f"{base}/api/models", timeout=timeout) as resp:
            models = json.loads(resp.read().decode() or "[]")
        out["models"] = [m["name"] if isinstance(m, dict) else m for m in models]
    except Exception as exc:  # noqa: BLE001
        out["models_error"] = f"{type(exc).__name__}: {exc}"
    return out


def parse_checks(text: str) -> tuple[list[dict], str | None]:
    checks: list[dict] = []
    result = None
    for line in text.splitlines():
        m = CHECK_RE.match(line)
        if m:
            status, rest = m.group(1), m.group(2)
            name, _, detail = rest.partition("  | ")
            checks.append({"status": status, "name": name.strip(), "detail": detail.strip()})
            continue
        m = RESULT_RE.match(line)
        if m:
            result = m.group(1)
    return checks, result


def script_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def run_suite(name: str, python: str, base: str) -> dict:
    script_name, desc, needs_model, needs_browser = SUITES[name]
    script = HERE / script_name

    record: dict = {
        "suite": name,
        "description": desc,
        "script": f"tools/qa/{script_name}",
        "script_sha256_16": script_hash(script),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": python,
        "service": probe_service(base),
    }

    if not record["service"]["healthy"]:
        record.update(result="BLOCKED", reason="服务不可达，前置条件不满足", totals={}, checks=[])
        print(f"\n[{name}] 阻断：{base} 上没有健康的服务。")
        print(f"  原因：{record['service'].get('health_error')}")
        print(f"  请先起服务：{START_HINT}")
        return record

    if needs_model and not record["service"]["models"]:
        record.update(result="BLOCKED", reason="没有可用模型，前置条件不满足", totals={}, checks=[])
        print(f"\n[{name}] 阻断：/api/models 为空，需要配置真实模型（models.toml + DEEPSEEK_API_KEY）。")
        return record

    if needs_browser:
        probe = subprocess.run(
            [python, "-c", "import playwright"], cwd=REPO_ROOT, capture_output=True, text=True
        )
        if probe.returncode != 0:
            record.update(result="BLOCKED", reason="playwright 不可用", totals={}, checks=[])
            print(f"\n[{name}] 阻断：{python} 里没有 playwright。浏览器套件要用项目 .venv 的解释器。")
            return record

    if needs_model:
        print(f"\n[{name}] 注意：本套件会真实调用模型（{record['service']['models'][0]}）。")

    print(f"\n{'=' * 72}\n[{name}] {desc}\n{'=' * 72}")
    t0 = time.time()
    proc = subprocess.run(
        [python, str(script)], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    elapsed = round(time.time() - t0, 1)
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output, end="" if output.endswith("\n") else "\n")

    checks, result = parse_checks(output)
    failed = [c for c in checks if c["status"] == "FAIL"]
    advisory = [c for c in checks if c["status"] == "ADVISORY"]
    skipped = [c for c in checks if c["status"] == "SKIP"]
    passed = [c for c in checks if c["status"] == "PASS"]

    if result is None:
        if not checks:
            result = "NO CHECKS PARSED"
        else:
            result = "HAS FAILURE" if failed else "ALL PASS"

    record.update(
        finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        duration_s=elapsed,
        exit_code=proc.returncode,
        totals={
            "total": len(checks),
            "passed": len(passed),
            "failed": len(failed),
            "advisory": len(advisory),
            "skipped": len(skipped),
        },
        checks=checks,
        result=result,
        model_used=record["service"]["models"][0] if record["service"]["models"] else None,
    )
    if not checks:
        record["reason"] = "没有解析到任何检查项——脚本输出格式变了，或脚本提前退出。"
    if skipped:
        record["skipped_reason"] = "缺 fixture（本机历史数据不足），非渲染逻辑失败"
    return record


START_HINT = (
    ".venv/Scripts/python.exe -m uvicorn --app-dir backend mini_agent.api:app "
    "--host 127.0.0.1 --port 8000   （必须在仓库根目录下执行）"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="mini-agent QA 统一入口")
    parser.add_argument("suite", nargs="?", default="list", help="套件名，或 list / all")
    parser.add_argument("--python", default=None, help="解释器路径（默认用项目 .venv）")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--with-boundary", action="store_true", help="让 all 也包含 boundary")
    parser.add_argument("--no-evidence", action="store_true", help="只跑，不写证据文件")
    args = parser.parse_args()

    if args.suite == "list":
        print("可用套件：\n")
        for name, (script, desc, needs_model, _) in SUITES.items():
            tag = "真实模型" if needs_model else "不调模型"
            heavy = "  ← all 默认跳过（代价高）" if name in HEAVY else ""
            print(f"  {name:<14} {desc}\n{'':<16}{tag} · {script}{heavy}")
        print(f"\n证据目录：tools/qa/evidence/\n启动服务的命令：{START_HINT}")
        return 0

    if args.suite == "all":
        names = [n for n in SUITES if args.with_boundary or n not in HEAVY]
    elif args.suite in SUITES:
        names = [args.suite]
    else:
        print(f"未知套件：{args.suite}（可用：{', '.join(SUITES)}, list, all）", file=sys.stderr)
        return 2

    python = pick_python(args.python)
    rev, dirty = git_rev()
    print(f"仓库 {REPO_ROOT}\n解释器 {python}\n提交 {rev}{'（工作区有未提交改动）' if dirty else ''}")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for name in names:
        record = run_suite(name, python, args.base_url)
        records.append(record)
        if not args.no_evidence:
            path = EVIDENCE_DIR / f"{name}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"→ 证据已写入 {path.relative_to(REPO_ROOT)}")

    print(f"\n{'=' * 72}\n汇总（提交 {rev}{'，工作区脏' if dirty else ''}）\n{'=' * 72}")
    blocked = 0
    for record in records:
        totals = record.get("totals") or {}
        if record["result"] == "BLOCKED":
            blocked += 1
            print(f"  {record['suite']:<14} 阻断  {record.get('reason', '')}")
        else:
            extra = ""
            if totals.get("failed"):
                extra += f"，{totals['failed']} 失败"
            if totals.get("skipped"):
                extra += f"，{totals['skipped']} 跳过（缺 fixture）"
            if totals.get("advisory"):
                extra += f"，{totals['advisory']} 观测项"
            print(
                f"  {record['suite']:<14} {record['result']:<12} "
                f"{totals.get('passed', 0)}/{totals.get('total', 0)} 通过{extra}"
            )

    failed = sum((r.get("totals") or {}).get("failed", 0) for r in records)
    return 1 if failed or blocked else 0


if __name__ == "__main__":
    sys.exit(main())
