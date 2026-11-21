# -*- coding: utf-8 -*-
"""
阶段四：打分算子抽检（生成待人工复核表）。

设计的核心：**抽检走 score.py 的真实算子**（`op_match_detail`），不复制逻辑。
否则抽的是副本，生产代码改了副本不会跟着变。

做法不是"预设标准答案再对答案"，而是：
  取 5 个代表不同分支的查询词 → 打印它在 45 条实验室上的**全部命中项 + 命中方式**
  → 人工只需判断"这条命中是真是假"，就能看出算子是否过宽/过窄。

为什么这样设计：方向算子的错误有两类，都必须靠人眼才看得见——
  · **过宽**：一个词命中十几条实验室（同义词组写得太松，如"数据分析"）
  · **过窄**：明明对口的实验室没命中（同义词组缺词，如补"集成电路"之前的芯片实验室）

用法：
    python scripts/spotcheck_scoring.py                  # 默认 5 个词
    python scripts/spotcheck_scoring.py --terms 视觉算法 边缘计算
产出：
    data/review/score_spotcheck.csv   （待人工填「判定 / 应得分 / 意见」）
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import op_match_detail, SYNONYMS

ROOT = Path(__file__).resolve().parents[1]
LABS = ROOT / "data" / "labs" / "labs.json"
OUT = ROOT / "data" / "review" / "score_spotcheck.csv"

# 5 个词覆盖不同分支：子串、同义词、疑似过宽、应全 0、跨域组合
DEFAULT_TERMS = ["视觉算法", "边缘计算", "数据分析", "量子计算", "医疗机器人"]

SEED = 20261119


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms", nargs="*", default=None)
    args = ap.parse_args()
    terms = args.terms or DEFAULT_TERMS

    labs = [l for l in json.loads(LABS.read_text(encoding="utf-8")) if not l.get("duplicate_of")]

    rows, summary = [], []
    for term in terms:
        hits = []
        for lab in labs:
            score, how, target = op_match_detail(term, lab.get("research_areas") or [])
            if score > 0:
                hits.append((lab, score, how, target))
        # 命中率预警：一个词命中太多实验室，多半是同义词过宽
        rate = len(hits) / len(labs)
        warn = "⚠ 疑似过宽" if rate >= 0.25 else ("⚠ 零命中(核对是否漏词)" if not hits else "")
        summary.append((term, len(hits), rate, warn))

        hits.sort(key=lambda x: -x[1])
        for lab, score, how, target in hits:
            # 同义词命中但字面完全不沾 → 重点复核对象
            literal = any(ch in target or term in target for ch in term)
            rows.append({
                "查询词": term,
                "lab_id": lab["lab_id"],
                "实验室名称": lab["name"],
                "机算分": score,
                "命中方式": how,
                "命中的研究方向": target,
                "字面沾边": "是" if literal else "否（纯同义/近义）",
                "该室方向全貌": "；".join(lab.get("research_areas") or [])[:110],
                "人工判定(对/错/存疑)": "",
                "应得分(0/0.6/0.8/1.0)": "",
                "人工意见": "",
            })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    header = list(rows[0].keys())
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)

    print("=== 命中概况（先看这一张）===")
    print(f'{"查询词":10s} {"命中":>4s} {"命中率":>7s}  预警')
    for term, n, rate, warn in summary:
        print(f"{term:10s} {n:4d} {rate:7.0%}  {warn}")

    print(f"\n共 {len(rows)} 行待判，已写入 {OUT.relative_to(ROOT)}")
    print(""
          "填写说明：\n"
          "  · 人工判定：这条命中对不对（对 / 错 / 存疑）\n"
          "  · 应得分：你认为该给几分（0 / 0.6 / 0.8 / 1.0）\n"
          "  · 人工意见：判「错」时必须写依据（引用研究方向原文）\n"
          "重点看『字面沾边=否』的行——纯同义词命中是最容易出错的地方。")
    print("\n同义词组规模（过大易过宽）：")
    for k, v in sorted(SYNONYMS.items(), key=lambda x: -len(x[1])):
        print(f"  {k:8s} {len(v):2d} 个词")


if __name__ == "__main__":
    main()
