# -*- coding: utf-8 -*-
"""
教师端指标评测：G-04a / G-04b / G-05。

G-04a（10 条可抽取）：field 与 lab_id 均正确的比例，目标 ≥9/10
G-04b（3 条含混）：不生成提案、转追问的比例，目标 3/3
G-05（回滚）：回滚后与快照逐字一致，目标 100%

用法： python scripts/teacher_eval.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import teacher  # noqa: E402

INS = ROOT / "data/eval/instructions.json"


def expected_fields(x):
    e = x.get("expected")
    if isinstance(e, list):
        return {p.get("field") for p in e if isinstance(p, dict)}
    return set()


def main():
    data = json.loads(INS.read_text(encoding="utf-8"))
    ok_a = n_a = 0
    ok_b = n_b = 0
    rows = []

    for x in data:
        typ, lab = x["type"], x["lab_id"]
        r = teacher.propose(x["utterance"], lab)

        if typ == "明确可抽取":
            n_a += 1
            got = {p["field"] for p in r.get("proposals", [])} if r.get("ok") else set()
            labs_ok = all(p["lab_id"] == lab for p in r.get("proposals", [])) if r.get("ok") else False
            hit = r.get("ok") and got == expected_fields(x) and labs_ok
            ok_a += hit
            rows.append((x["id"], "G-04a", hit,
                         f'期望 {sorted(expected_fields(x))} 实得 {sorted(got)}'
                         + ("" if r.get("ok") else f' [{r.get("code")}]')))

        elif typ == "含混":
            n_b += 1
            hit = (not r.get("ok")) and r.get("code") == "clarify"
            ok_b += hit
            rows.append((x["id"], "G-04b", hit,
                         "追问" if hit else f'生成了提案 [{r.get("code")}]'))

        else:   # 应澄清（目标不存在）
            hit = (not r.get("ok")) and str(r.get("code", "")).startswith(("clarify", "value_not_found"))
            rows.append((x["id"], "T-06", hit,
                         str(r.get("question") or r.get("code"))[:40]))

    print(f'{"用例":6s} {"指标":7s} {"结果":4s} 说明')
    for i, m, h, msg in rows:
        print(f'{i:6s} {m:7s} {"✅" if h else "❌":4s} {msg}')

    print(f"\n=== G-04a 字段准确率 ===\n  {ok_a}/{n_a}"
          f'（目标 ≥9/10）{"✅" if ok_a >= 9 else "❌"}')
    print(f"=== G-04b 正确追问率 ===\n  {ok_b}/{n_b}"
          f'（目标 3/3）{"✅" if ok_b == 3 else "❌"}')

    # ---- G-05 回滚一致率 ----
    print("\n=== G-05 回滚一致性 ===")
    before = teacher.LABS.read_bytes()
    r = teacher.apply("我们今年拿了 RoboMaster 全国赛一等奖", "L01", confirmed=True)
    if not r.get("ok"):
        print(f"  ❌ 写入失败：{r.get('code')}")
        return
    sid = r["snapshot"]
    changed = teacher.LABS.read_bytes() != before
    rb = teacher.rollback(sid)
    restored = teacher.LABS.read_bytes() == before
    print(f"  写入生效: {'✅' if changed else '❌'}（快照 {sid}）")
    print(f"  回滚一致: {'✅' if rb.get('identical') and restored else '❌'}"
          f'（逐字比对快照 {len(before)} 字节）')
    print(f"=== G-05 ===\n  {1 if (rb.get('identical') and restored) else 0}/1"
          f'（目标 100%）{"✅" if rb.get("identical") and restored else "❌"}')


if __name__ == "__main__":
    main()
