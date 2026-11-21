# -*- coding: utf-8 -*-
"""
复核表回填后的校验与落盘。

流程：
  1. 读 data/review/*.csv（复核人填完的三列：判定 / 改判为 / 依据）
  2. 校验：是否全填、判 B 是否给了改判值和依据、判 C 是否列了待议点
  3. 汇总：A/B/C 分布、改判清单
  4. 加 --apply 时：备份原文件 → 应用改判 → 重跑 check_evalset

用法：
    python scripts/apply_review.py                        # 只校验与汇总（三张表都要填完）
    python scripts/apply_review.py --only path_type       # 只校验某一张表（阶段一先完成时用）
    python scripts/apply_review.py --only path_type --apply  # 落盘该表并重跑自检
"""
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_review_table import read_table   # 兼容 CSV 与「Excel 另存但扩展名仍为 .csv」的 xlsx

ROOT = Path(__file__).resolve().parents[1]
REV = ROOT / "data" / "review"
PT = ROOT / "data" / "labs" / "path_type.json"
Q = ROOT / "data" / "eval" / "queries.json"

VALID = {"A", "B", "C"}
PATH_TYPES = {"竞赛主导", "科研主导", "双主导", "就业项目主导", "教学平台", "待确认"}


def read(name):
    _, rows = read_table(REV / name)
    return rows


def norm_verdict(v):
    return (v or "").strip().upper()[:1]


def check_path_type(rows):
    errs, changes, pend = [], [], []
    for r in rows:
        lid = r["lab_id"]
        v = norm_verdict(r.get("复核判定(A维持/B改判/C存疑)"))
        why = (r.get("复核依据（引用原文≥15字）") or "").strip()
        new = (r.get("改判为") or "").strip()
        if not v:
            errs.append(f"{lid} 未填复核判定")
            continue
        if v not in VALID:
            errs.append(f"{lid} 判定值非法：{v}（应为 A/B/C）")
            continue
        if v in ("B", "C") and len(why) < 15:
            errs.append(f"{lid} 判 {v} 但依据不足 15 字（当前 {len(why)}）")
        if v == "B":
            if new not in PATH_TYPES:
                errs.append(f"{lid} 判 B 但改判值非法：{new or '(空)'}")
            else:
                changes.append((lid, r["当前判定"], new, why))
        if v == "C":
            pend.append((lid, r["当前判定"], why))
    return errs, changes, pend


def check_evalset(rows, kind):
    errs, changes, pend = [], [], []
    for r in rows:
        qid = r["id"]
        v = norm_verdict(r.get("复核判定(A维持/B改判/C存疑)"))
        if not v:
            errs.append(f"{qid} 未填复核判定")
            continue
        if v == "B":
            sug = (r.get("逐条合理性确认意见") or r.get("复核意见") or "").strip()
            if len(sug) < 10:
                errs.append(f"{qid} 判 B 但没写修改建议")
            else:
                changes.append((qid, sug))
        if v == "C":
            pend.append((qid, (r.get("逐条合理性确认意见") or r.get("复核意见") or "").strip()))
        if kind == "q" and v == "A":
            # 维持也必须留确认意见：证明是读过 research_areas 的，不是空过
            op = (r.get("逐条合理性确认意见") or "").strip()
            if len(op) < 10:
                errs.append(f"{qid} 判 A 但未写「逐条合理性确认意见」")
    return errs, changes, pend


def main():
    apply = "--apply" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    pt_rows = read("path_type_review.csv") if only in (None, "path_type") else []
    q_rows = read("evalset_q_review.csv") if only in (None, "q") else []
    t_rows = read("evalset_t_review.csv") if only in (None, "t") else []

    if only:
        print(f"（只校验 {only} 表，其余表暂不参与）\n")

    all_errs = []
    print(f"=== 表 1：path_type（{len(pt_rows)} 条）===") if pt_rows else None
    e, ch, pe = check_path_type(pt_rows)
    all_errs += e
    print(f"  A 维持 {sum(1 for r in pt_rows if norm_verdict(r.get('复核判定(A维持/B改判/C存疑)'))=='A')} | "
          f"B 改判 {len(ch)} | C 存疑 {len(pe)}")
    for x in ch:
        print(f"    改判 {x[0]}: {x[1]} → {x[2]}｜{x[3][:40]}…")

    print(f"\n=== 表 2：评测集 Q（{len(q_rows)} 条）===") if q_rows else None
    e2, ch2, pe2 = check_evalset(q_rows, "q")
    all_errs += e2
    if q_rows:
        print(f"  A {sum(1 for r in q_rows if norm_verdict(r.get('复核判定(A维持/B改判/C存疑)'))=='A')} | "
              f"B {len(ch2)} | C {len(pe2)}")
    for x in ch2:
        print(f"    修改 {x[0]}: {x[1][:50]}")

    print(f"\n=== 表 3：评测集 T（{len(t_rows)} 条）===") if t_rows else None
    e3, ch3, pe3 = check_evalset(t_rows, "t")
    all_errs += e3
    if t_rows:
        print(f"  A {sum(1 for r in t_rows if norm_verdict(r.get('复核判定(A维持/B改判/C存疑)'))=='A')} | "
              f"B {len(ch3)} | C {len(pe3)}")

    print("\n=== 校验 ===")
    if all_errs:
        print(f"  ❌ {len(all_errs)} 项待处理：")
        for x in all_errs[:20]:
            print("   -", x)
        if not apply:
            print("\n（未加 --apply，未改动任何文件）")
            return
    else:
        scope = only or "三张表"
        print(f"  ✅ {scope} 校验通过")

    if pe or pe2 or pe3:
        print(f"  ⚠ 存疑挂起 {len(pe)+len(pe2)+len(pe3)} 项，需第三人裁决后才能冻结")

    if not apply:
        print("\n（未加 --apply，未改动任何文件）")
        return

    # ---------- 落盘 ----------
    stamp = ".bak"
    shutil.copy(PT, PT.with_suffix(".json" + stamp))
    data = json.loads(PT.read_text(encoding="utf-8"))
    for lid, old, new, why in ch:
        data[lid]["path_type"] = [new]
        data[lid]["source"] = "review"
        data[lid]["evidence"] = {"复核改判": why}
    PT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已应用 {len(ch)} 条改判 → {PT.name}（备份 {PT.name}.bak）")

    # ⚠ 关键连带：score.py 读的是 labs.json，而 labs.json 的 path_type 由 inject_l2.py
    # 从 path_type.json 复制而来。不同步这一步，改判后分数不会变（静默失效）。
    print("\n=== 同步 labs.json（否则 score.py 仍用旧 path_type）===")
    r2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "inject_l2.py")],
                        capture_output=True, text=True, encoding="utf-8")
    synced = [l for l in r2.stdout.splitlines() if l.startswith(("记录数", "path_type", "R2 分布"))]
    print("\n".join(synced) if synced else r2.stdout[-400:] or r2.stderr[-400:])

    # 评测集改判是自由文本建议，无法安全自动应用 → 导出清单由人工落到 JSON
    todo = REV / "todo_evalset.md"
    lines = ["# 评测集待改清单（由 apply_review.py 自动生成）", "",
             "> 评测集的 B/C 项是自然语言建议，脚本不自动应用。",
             "> 请按下列清单手工修改对应 JSON，然后重跑 check_evalset.py。", ""]
    if ch2 or ch3:
        lines += ["## 需修改（B 改判）", "",
                  "| 条目 | 目标文件 | 修改建议 |", "|---|---|---|"]
        for qid, sug in ch2:
            lines.append(f"| {qid} | `data/eval/queries.json` | {sug} |")
        for tid, sug in ch3:
            lines.append(f"| {tid} | `data/eval/instructions.json` | {sug} |")
    if pe2 or pe3:
        lines += ["", "## 存疑挂起（C，需第三人裁决）", "",
                  "| 条目 | 待议点 |", "|---|---|"]
        for qid, sug in pe2 + pe3:
            lines.append(f"| {qid} | {sug} |")
    if not (ch2 or ch3 or pe2 or pe3):
        lines += ["（无待改项）"]
    todo.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n评测集待改清单已写入 {todo.relative_to(ROOT)}")

    print("\n=== 重跑评测集自检（改判可能影响合理选择集）===")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_evalset.py")],
                       capture_output=True, text=True, encoding="utf-8")
    tail = [l for l in r.stdout.splitlines() if l.startswith(("自检结果", "  ❌", "  ✅ G-01"))]
    print("\n".join(tail) or r.stdout[-500:])


if __name__ == "__main__":
    main()
