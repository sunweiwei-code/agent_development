# -*- coding: utf-8 -*-
"""
端到端因果链：老师改了档案 → 学生端检索立刻变。

为什么单独一套：G-04 证明"写入正确"，G-01 证明"检索正确"，但
**"改了以后学生端到底变没变"从来没被验证过**。而这里恰恰踩过坑——
sync_index 删掉孤儿向量却没补齐，检索直接降级，当时单测是全绿的。
两端各自的绿灯，拼不出这条链的绿灯。

断言四件事（缺一不可）：
  ① 改前：这个话题搜不到该实验室（否则说明不了因果）
  ② 改后：同一句话能搜到（检索 + 打分 + 重排都认了）
  ③ 改后：学生端详情里能读到新内容（不是只有索引变了，档案也变了）
  ④ 回滚后：又搜不到（排除"其实一直是缓存/一直是命中"的假象）

用法： python tests/e2e_chain_test.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline   # noqa: E402
import teacher    # noqa: E402

LAB = "L04"                 # 无人机协会
AREA = "农业植保无人机"      # 全库 45 条里不存在的方向词
ASK = "我想做农业植保无人机"
DETAIL_ASK = "介绍一下无人机协会"

ok = total = 0


def check(name, cond, detail=""):
    global ok, total
    total += 1
    ok += bool(cond)
    print(f'  {"✅" if cond else "❌"} {name:38s} {detail}')


def ask(q):
    r = pipeline.recommend(q)
    return ([t["lab_id"] for t in r["top3"]],
            [o["lab_id"] for o in r.get("others") or []],
            r)


def main():
    labs = teacher.load_labs()
    l04 = next(l for l in labs if l["lab_id"] == LAB)

    print("前置：确认这个话题在库里确实是空白")
    word_hit = [l["lab_id"] for l in labs
                if any("植保" in str(l.get(f, "")) for f in
                       ("research_areas", "intro", "direction_notes", "name"))]
    check("全库无人提过「植保」", not word_hit, f"命中 {word_hit}")

    # ---- ① 改前 ----
    top, others, r1 = ask(ASK)
    check("改前 L04 搜不到", LAB not in top and LAB not in others,
          f'形态={r1["form"]} top3={top}')

    # ---- 教师写入 ----
    print(f"\n教师端：给 {LAB} 增加研究方向「{AREA}」")
    res = teacher.apply(f"研究方向增加{AREA}", LAB, confirmed=True)
    check("写入成功并留快照", res.get("ok"), f'快照={res.get("snapshot")}')
    if not res.get("ok"):
        print("写入失败，后续断言无从谈起")
        return 1
    sid = res["snapshot"]

    try:
        # ---- ② 改后：检索能召回 ----
        top2, others2, r2 = ask(ASK)
        check("改后 L04 进 top3", LAB in top2,
              f'形态={r2["form"]} top3={top2} others={others2}')

        # ---- ③ 改后：详情能读到新内容 ----
        r3 = pipeline.recommend(DETAIL_ASK)
        lab_now = r3.get("lab") or {}
        check("学生端详情读到新方向", AREA in (lab_now.get("research_areas") or []),
              f'形态={r3["form"]}')
    finally:
        rb = teacher.rollback(sid)

    # ---- ④ 回滚后：立刻失效 ----
    check("回滚逐字一致", rb.get("identical"), f'identical={rb.get("identical")}')
    top4, others4, r4 = ask(ASK)
    check("回滚后 L04 又搜不到", LAB not in top4 and LAB not in others4,
          f'形态={r4["form"]} top3={top4}')

    # 档案本身也要回到原样，不能只回滚了索引
    l04_after = next(l for l in teacher.load_labs() if l["lab_id"] == LAB)
    check("档案恢复原值", l04_after["research_areas"] == l04["research_areas"],
          f'{l04_after["research_areas"]}')

    print(f"\n端到端因果链：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
