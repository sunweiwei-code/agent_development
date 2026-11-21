# -*- coding: utf-8 -*-
"""
打分算子回归测试（用例来自阶段四人工复核：data/eval/scoring_cases.json）。

用途：任何对 score.py 的改动，都要跑这个。人工判定就是"标准答案"，
改完必须让通过条数上升、且不得让原本通过的行变红。

用法：
    python scripts/test_scoring.py           # 汇总
    python scripts/test_scoring.py -v        # 逐条
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import op_match_detail

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "data" / "eval" / "scoring_cases.json").read_text(encoding="utf-8"))
TOL = 1e-6


def main():
    verbose = "-v" in sys.argv
    ok, bad = [], []
    for c in CASES:
        score, how, _ = op_match_detail(c["query"], [c["target"]])
        passed = abs(score - c["expected"]) < TOL
        (ok if passed else bad).append((c, score, how))

    print(f"回归结果：通过 {len(ok)}/{len(CASES)}")
    if bad:
        print("\n未通过明细：")
        for c, score, how in bad:
            arrow = "偏高" if score > c["expected"] else "偏低"
            print(f'  {c["query"]:8s} {c["lab_id"]} 机{score} vs 应{c["expected"]}（{arrow}）'
                  f'  {how:12s} ← {c["target"][:26]}')
            if c["note"]:
                print(f'      人工意见: {c["note"][:80]}')

    if verbose:
        print("\n全部用例：")
        for c in CASES:
            score, how, _ = op_match_detail(c["query"], [c["target"]])
            mark = "✅" if abs(score - c["expected"]) < TOL else "❌"
            print(f'  {mark} {c["query"]:8s} {c["lab_id"]} 机{score} 应{c["expected"]} {how:12s} ← {c["target"][:24]}')

    # 按查询词看哪一类最容易错
    if bad:
        print("\n按查询词的失败分布：", dict(Counter(c["query"] for c, _, _ in bad)))
        print("按命中方式的失败分布：", dict(Counter(how.split("=")[0] for _, _, how in bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
