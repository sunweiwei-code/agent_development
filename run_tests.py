# -*- coding: utf-8 -*-
"""
一键回归：把十二套用例串起来跑，末尾给汇总表。

    python run_tests.py            # 全跑（含 UI 与教师链路，约 12–16 分钟）
    python run_tests.py --fast     # 跳过 UI 与教师链路（约 2 分钟）

退出码：0 全过；1 有失败（可直接用于 CI 或提交前检查）。

为什么要有这个：之前每次改完都是零散地敲五六条命令，靠眼睛看输出。
漏跑一套就等于没测——形态③的覆盖断掉那次，就是因为只跑了单点测试。
"""
import atexit
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

CASES = [
    # name, 命令, 判定函数（返回 True 表示通过）, 是否 fast
    ("规则层 33 算子", ["scripts/test_scoring.py"],
     lambda out: re.search(r"通过\s*(\d+)/(\d+)", out) and
     (lambda m: m.group(1) == m.group(2))(re.search(r"通过\s*(\d+)/(\d+)", out)), True),

    ("评测集完整性", ["scripts/check_evalset.py"],
     lambda out: "全部通过" in out, True),

    ("学生链路 G-01/01b/02/03", ["scripts/run_eval.py"],
     lambda out: all(s in out for s in ("6/6", "2/2", "5/5", "幻觉率 0.0%")), True),

    ("教师链路 G-04a/04b/05", ["scripts/teacher_eval.py"],
     lambda out: all(s in out for s in ("10/10", "3/3", "1/1")), False),

    ("闸门反测试", ["tests/teacher_gate_test.py"],
     lambda out: re.search(r"(\d+)/(\d+) 通过", out) and
     (lambda m: m.group(1) == m.group(2))(re.search(r"(\d+)/(\d+) 通过", out)), False),

    ("端到端因果链", ["tests/e2e_chain_test.py"],
     lambda out: re.search(r"(\d+)/(\d+) 通过", out) and
     (lambda m: m.group(1) == m.group(2))(re.search(r"(\d+)/(\d+) 通过", out)), False),

    ("UI 端到端 9 场景", ["tests/ui_test.py"],
     # 不写死通过条数：加一条用例就得回来改这里，容易忘（这次就忘了）
     lambda out: bool(re.search(r"UI 测试：\d+ 通过 / 0 失败", out)), False),

    ("教师端 UI 6 场景", ["tests/teacher_ui_test.py"],
     lambda out: "0 失败" in out and "6 通过" in out, False),

    ("档案页 8 场景", ["tests/lab_test.py"],
     lambda out: "0 失败" in out and "8 通过" in out, False),

    ("通道拓扑 10 场景", ["tests/nav_test.py"],
     lambda out: "0 失败" in out and "通道测试" in out, False),

    ("跨会话记忆 14 项", ["tests/memory_test.py"],
     lambda out: "0 失败" in out and "记忆测试" in out, True),

    ("首页气泡 7 项", ["tests/home_test.py"],
     lambda out: "0 失败" in out and "首页气泡测试" in out, True),
]


def _port_open(port=8000, host="127.0.0.1"):
    try:
        socket.create_connection((host, port), 1).close()
        return True
    except OSError:
        return False


def _ensure_server():
    """用例大多要走 HTTP（127.0.0.1:8000），没起服务就跑=一片红，且看不出原因。

    端口占用时就当是用户自己开着的，跑完不动它；没占用就自己起一个，结束时收掉——
    这样 clone 下来直接 `python run_tests.py` 也能跑，不用先另开一个终端。
    """
    if _port_open():
        return None
    log = ROOT / "tmp_dl" / "run_tests_server.log"
    log.parent.mkdir(exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        proc = subprocess.Popen([PY, "server/app.py"], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(40):
        time.sleep(0.25)
        if _port_open():
            print("（没有检测到服务，已自动起在 127.0.0.1:8000）")
            atexit.register(proc.terminate)      # 正常结束或 Ctrl+C 都收掉，不留孤儿进程
            return proc
        if proc.poll() is not None:
            break
    print(f"服务起不来，看 {log}")
    proc.terminate()
    return None


def run(cmd, timeout):
    p = subprocess.run([PY] + cmd, cwd=ROOT, capture_output=True, timeout=timeout)
    out = (p.stdout or b"").decode("utf-8", "ignore") + (p.stderr or b"").decode("utf-8", "ignore")
    return p.returncode, out


def main():
    fast = "--fast" in sys.argv
    rows, bad = [], 0
    srv = _ensure_server()
    if srv is None and not _port_open():
        return 1

    for name, cmd, ok, is_fast in CASES:
        if fast and not is_fast:
            rows.append((name, "跳过", "-"))
            continue
        print(f"\n{'='*60}\n▶ {name}\n{'='*60}")
        try:
            code, out = run(cmd, 600)
            tail = "\n".join(out.strip().splitlines()[-14:])
            print(tail)
            passed = (code == 0) and bool(ok(out))
        except subprocess.TimeoutExpired:
            passed, out = False, "超时"
            print("超时")
        rows.append((name, "✅ 通过" if passed else "❌ 失败", "" if passed else "见上方输出"))
        bad += (not passed)

    print(f"\n{'='*60}\n汇总\n{'='*60}")
    for n, s, note in rows:
        print(f"  {s}  {n} {note}")
    print(f"\n{'全部通过' if bad == 0 else f'有 {bad} 套失败'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
