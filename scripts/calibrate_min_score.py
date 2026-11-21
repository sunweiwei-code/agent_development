# -*- coding: utf-8 -*-
"""
M0：MIN_SCORE 标定（PRD 8.1 标定流程）。

硬约束（评审明确）：
  - **只用 45 条库数据 + 合成画像**，禁止使用 Q-01~13 / T-01~13 评测集
  - 合成画像 20–30 个，覆盖：仅方向 / 仅 goal / 方向+技术栈 / 全维度 / 方向模糊
  - 输出：分数分布（直方图 + P25/P50/P75）+ 建议阈值 + 各候选阈值下的形态分布

判据（8.1）：
  - 全维度命中的画像能进 Top3（形态①）
  - 仅单维度模糊命中（方向算子 0.6）的画像被挡（形态③）或走路径 A
"""
import json
import statistics
from pathlib import Path

from score import rank, LABS

# 合成画像：与评测集 Q-01~13 无重叠，仅用于看分数分布
PROFILES = [
    # --- A 全维度（5）---
    ("A1", {"interest": "视觉识别", "goal": "竞赛", "tech_stack": "Python", "grade": "大一", "target_contest": "RoboMaster"}),
    ("A2", {"interest": "自然语言处理", "goal": "科研", "tech_stack": "PyTorch", "grade": "大三"}),
    ("A3", {"interest": "嵌入式开发", "goal": "就业", "tech_stack": "C语言", "grade": "大二", "preferred_teacher": "宁磊"}),
    ("A4", {"interest": "数据挖掘", "goal": "科研", "tech_stack": "机器学习", "grade": "大二"}),
    ("A5", {"interest": "物联网", "goal": "就业", "tech_stack": "C语言", "grade": "大一"}),
    # --- B 方向 + goal（4）---
    ("B1", {"interest": "网络安全", "goal": "科研"}),
    ("B2", {"interest": "机器人", "goal": "竞赛"}),
    ("B3", {"interest": "云计算", "goal": "就业"}),
    ("B4", {"interest": "信号处理", "goal": "科研"}),
    # --- C 仅方向（精确/同义）（4）---
    ("C1", {"interest": "计算机视觉"}),
    ("C2", {"interest": "大语言模型"}),
    ("C3", {"interest": "集成电路"}),
    ("C4", {"interest": "生物医学"}),
    # --- D 方向+技术栈（3）---
    ("D1", {"interest": "算法", "tech_stack": "C++"}),
    ("D2", {"interest": "鸿蒙", "tech_stack": "ArkTS"}),
    ("D3", {"interest": "芯片", "tech_stack": "Verilog"}),
    # --- E 仅 goal（3）---
    ("E1", {"goal": "科研"}),
    ("E2", {"goal": "竞赛"}),
    ("E3", {"goal": "就业"}),
    # --- F 方向模糊（4）：刻意选库内不存在的方向 ---
    ("F1", {"interest": "量子计算"}),
    ("F2", {"interest": "区块链"}),
    ("F3", {"interest": "图形学渲染"}),
    ("F4", {"interest": "航空航天"}),
    # --- G 空画像（1）---
    ("G1", {}),
]

THRESHOLDS = [0.25, 0.30, 0.35, 0.40, 0.45]


def main():
    labs = json.loads(LABS.read_text(encoding="utf-8"))
    rows = []
    for pid, prof in PROFILES:
        ranked = rank(prof, labs)
        d_all = [s["direction"] for _, s in ranked]
        # 路径 A：interest 非空 且 全部候选方向分为 0
        path_a = bool(prof.get("interest")) and max(d_all) == 0
        # 方向门禁：方向分为 0 的候选不得进入 Top3（V2.9 新增必要条件）
        elig = [(l, s) for l, s in ranked if s["direction"] > 0]
        top = elig[0] if elig else (ranked[0][0], {"score": 0.0, "direction": 0.0})
        rows.append({
            "id": pid, "profile": prof,
            "top1": top[1]["score"], "top1_lab": top[0]["name"],
            "top3": [s["score"] for _, s in elig[:3]],
            "d_top1": top[1]["direction"], "d_max": max(d_all),
            "path_a": path_a,
            "no_dir": not bool(prof.get("interest")),   # 没给方向 → 追问或形态④
            "elig_n": len(elig),
        })

    print("=== 各画像的分数分布 ===")
    print(f'{"ID":4s} {"interest":10s} {"goal":4s} {"top1":>6s} {"top3":>18s} {"d(top1)":>7s} {"路径A":>5s}  top1 实验室')
    for r in rows:
        it = r["profile"].get("interest") or "—"
        gl = r["profile"].get("goal") or "—"
        t3 = "/".join(f"{x:.2f}" for x in r["top3"])
        print(f'{r["id"]:4s} {it:10s} {gl:4s} {r["top1"]:6.3f} {t3:>18s} {r["d_top1"]:7.1f} {"是" if r["path_a"] else "否":>5s}  {r["top1_lab"][:16]}')

    tops = [r["top1"] for r in rows if not r["path_a"] and not r["no_dir"]]
    tops_sorted = sorted(tops)
    def pct(p):
        k = max(0, min(len(tops_sorted) - 1, int(round(p * (len(tops_sorted) - 1)))))
        return tops_sorted[k]
    print("\n=== top1 分数分位（排除路径 A 的画像，n=%d）===" % len(tops))
    print(f"  min {min(tops):.3f} | P25 {pct(.25):.3f} | P50 {pct(.50):.3f} | P75 {pct(.75):.3f} | max {max(tops):.3f}")
    hist = {}
    for t in tops:
        b = round(int(t * 20) / 20, 2)
        hist[b] = hist.get(b, 0) + 1
    print("  直方图(0.05 分箱):", dict(sorted(hist.items())))

    print("\n=== 候选阈值下的形态分布（已加方向门禁：方向=0 的候选不进 Top3）===")
    print(f'{"MIN":>5s} {"形态①(有达标)":>12s} {"形态③(全挡)":>11s} {"路径A":>6s}  被挡画像')
    for th in THRESHOLDS:
        n1 = sum(1 for r in rows if not r["path_a"] and not r["no_dir"] and r["top1"] >= th)
        n3 = sum(1 for r in rows if not r["path_a"] and not r["no_dir"] and r["top1"] < th)
        na = sum(1 for r in rows if r["path_a"])
        blocked = [r["id"] for r in rows if not r["path_a"] and not r["no_dir"] and r["top1"] < th]
        print(f"{th:5.2f} {n1:12d} {n3:11d} {na:6d}  {','.join(blocked) or '—'}")

    print("\n=== 关键校验 ===")
    fuzzy = [r for r in rows if r["d_max"] and r["d_max"] <= 0.6]
    print("模糊方向画像(方向分≤0.6):", [(r["id"], r["d_max"], r["top1"]) for r in fuzzy] or "无")
    exact = [r for r in rows if r["d_max"] >= 0.8]
    print("精确/同义方向画像(≥0.8):", [(r["id"], r["d_max"], r["top1"]) for r in exact])
    print("路径 A 画像:", [r["id"] for r in rows if r["path_a"]])

    Path("data/labs/min_score_calibration.json").parent.mkdir(parents=True, exist_ok=True)
    Path("data/labs/min_score_calibration.json").write_text(
        json.dumps({"profiles": rows, "thresholds": THRESHOLDS}, ensure_ascii=False, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()
