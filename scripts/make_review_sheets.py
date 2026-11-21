# -*- coding: utf-8 -*-
"""
生成人工复核用的待填表（3 张 CSV，UTF-8-BOM 便于 Excel 直接打开）。

设计原则：
  1. 复核表里预填"当前判定 + 证据"，复核人只需填最后三列（判定 / 依据 / 改判值）
  2. 判据代码化：A 维持 / B 改判 / C 存疑（挂起，需第三人裁决）
  3. 表里**不放分数结论**（避免复核人被系统输出锚定），只放原文与计数依据

用法：python scripts/make_review_sheets.py
产出：data/review/path_type_review.csv
      data/review/evalset_q_review.csv
      data/review/evalset_t_review.csv
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from annotate_path_type import count_items   # 复用计数口径，保证复核表与判定同源

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "review"
OUT.mkdir(parents=True, exist_ok=True)

labs = {l["lab_id"]: l for l in json.loads(
    (ROOT / "data/labs/labs.json").read_text(encoding="utf-8"))}
pt = json.loads((ROOT / "data/labs/path_type.json").read_text(encoding="utf-8"))
Q = json.loads((ROOT / "data/eval/queries.json").read_text(encoding="utf-8"))
T = json.loads((ROOT / "data/eval/instructions.json").read_text(encoding="utf-8"))


FORCE = "--force" in sys.argv


def w(path, header, rows):
    # 已填过的表默认不覆盖：复核人可能用 Excel 存成了 xlsx（扩展名仍是 .csv），
    # 直接覆盖会丢掉已填内容。
    if path.exists() and not FORCE:
        print(f"  ⚠ 跳过 {path.name}（已存在；如要重建请加 --force）")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(header)
        cw.writerows(rows)
    print(f"  {path.name}: {len(rows)} 行")


# ---------- 表 1：path_type ----------
h1 = ["lab_id", "实验室名称", "研究方向（原文，用于判断）",
      "竞赛项", "科研项", "比值", "当前判定", "当前置信",
      "复核判定(A维持/B改判/C存疑)", "改判为", "复核依据（引用原文≥15字）",
      "复核人", "复核日期"]
r1 = []
for lid in sorted(pt):
    v, lab = pt[lid], labs[lid]
    comp_n, res_n, _ = count_items(lab.get("achievements") or [])
    ratio = round(comp_n / res_n, 2) if res_n else ("∞" if comp_n else "—")
    r1.append([
        lid, lab["name"],
        "；".join(lab.get("research_areas") or [])[:160],
        comp_n, res_n, ratio,
        v["path_type"][0], v["confidence"],
        "", "", "", "", "",
    ])
w(OUT / "path_type_review.csv", h1, r1)

# ---------- 表 2：评测集 Q 侧 ----------
h2 = ["id", "类型", "用户原话", "标注槽位", "期望形态",
      "合理选择集（lab_id:判定）", "已排除（lab_id，及排除理由摘要）",
      "复核判定(A维持/B改判/C存疑)", "逐条合理性确认意见", "复核人", "复核日期"]
r2 = []
for q in Q:
    rs = "；".join(f'{e["lab_id"]}:{e["verdict"]}' for e in q.get("reasonable_set", [])) or "—"
    ex = "；".join(f'{e["lab_id"]}（{e["reason"][:28]}…）' for e in q.get("excluded", [])) or "—"
    r2.append([q["id"], q["type"], q["utterance"],
               json.dumps(q.get("slots") or {}, ensure_ascii=False), q["expected_form"],
               rs, ex, "", "", "", ""])
w(OUT / "evalset_q_review.csv", h2, r2)

# ---------- 表 3：评测集 T 侧 ----------
h3 = ["id", "类型", "绑定lab_id", "教师原话", "期望 field/op/new_value",
      "复核判定(A维持/B改判/C存疑)", "op 是否属该字段允许集", "复核意见", "复核人", "复核日期"]
r3 = []
for t in T:
    exp = t["expected"]
    if isinstance(exp, str):
        exp_s = exp
    else:
        exp_s = " | ".join(f'{e["field"]}/{e["op"]}/{json.dumps(e["new_value"], ensure_ascii=False)}'
                           for e in exp)
    r3.append([t["id"], t["type"], t["lab_id"], t["utterance"], exp_s, "", "", "", "", ""])
w(OUT / "evalset_t_review.csv", h3, r3)

print(f"\n复核表已生成于 {OUT}")
print("填写后运行 python scripts/apply_review.py 做回填校验")
