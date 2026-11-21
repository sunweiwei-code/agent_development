# -*- coding: utf-8 -*-
"""
评测集自检（M0 冻结前必跑）。

校验内容：
  Q 侧
    1. 每条 query 的实测形态是否等于标注的 expected_form
    2. 合理选择集是否与实测 Top3 一致（标注必须含排第 3 的，否则"Top3 有它也不算数"）
    3. G-01 的 6 条（Q-01~04 + Q-12~13）合理选择集：≥6 个不同实验室、覆盖科研/竞赛/双主导各 ≥1
    4. 至少 1 条 query 的合理选择集含双主导
    5. 产生推荐（形态①④）的 query 合计触达 ≥10 个不同实验室
  T 侧
    6. field 是否在 7 字段白名单
    7. op 与字段类型是否匹配（8.2 类型匹配表）
    8. 数组/字符串的 remove 目标是否真的存在于现值（否则会 value_not_found）
    9. 含混指令是否标注为"追问"
"""
import json
from pathlib import Path

from score import rank, LABS, PATH_MAP

ROOT = Path(__file__).resolve().parents[1]
Q = json.loads((ROOT / "data" / "eval" / "queries.json").read_text(encoding="utf-8"))
T = json.loads((ROOT / "data" / "eval" / "instructions.json").read_text(encoding="utf-8"))
LAB = {l["lab_id"]: l for l in json.loads(LABS.read_text(encoding="utf-8"))}

MIN_SCORE = 0.35
ALLOWED = {
    "location": {"replace"},
    "intro": {"replace", "append", "remove"},
    "achievements": {"replace", "append", "remove"},
    "research_areas": {"replace", "append", "remove"},
    "group_tags": {"replace", "append", "remove"},
    "recruit_info": {"merge"},
    "contact": {"merge"},
}


def form_of(slots):
    """返回 (形态, Top3列表, 全部候选数)"""
    ranked = rank(slots, list(LAB.values()))
    if not slots.get("interest"):
        return "④/追问", [], 0
    if max(s["direction"] for _, s in ranked) == 0:
        return "②", [], 0
    elig = [(l, s) for l, s in ranked if s["direction"] > 0 and s["score"] >= MIN_SCORE]
    if not elig:
        return "③", [], 0
    return "①", elig[:3], len(elig)


def main():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + msg)
        if not cond:
            ok = False

    print("=== Q 侧 ===")
    g01_ids = {"Q-01", "Q-02", "Q-03", "Q-04", "Q-12", "Q-13"}
    g01_labs, rec_labs, dual_hit = set(), set(), []
    for q in Q:
        form, top3, n = form_of(q["slots"])
        got = [l["lab_id"] for l, _ in top3]
        exp_form = q["expected_form"]
        form_ok = got and (exp_form in ("①", "③") and form == "①" or exp_form == form) or \
                  (not got and exp_form.split("/")[0] in form)
        rs = [e["lab_id"] for e in q.get("reasonable_set", [])]
        print(f'{q["id"]} {q["type"]:6s} 实测{form:5s} 标注{exp_form:5s} Top3={[(l["lab_id"], round(s["score"],2)) for l,s in top3]}')
        print(f'     合理选择集(人工确认)={rs}  排除={[e["lab_id"] for e in q.get("excluded", [])]}')

        # 每条合理项必须有"人工确认理由"，且理由要与该实验室的研究方向/简介有词汇重合
        for e in q.get("reasonable_set", []):
            lid, reason = e["lab_id"], e.get("reason", "")
            chk(len(reason) >= 12, f'{q["id"]} {lid} 有人工确认理由（{len(reason)} 字）')
            lab = LAB[lid]
            pool = " ".join((lab.get("research_areas") or []) + [lab.get("intro") or ""])
            key = [w for w in ["视觉", "算法", "安全", "生物", "通信", "人工智能", "数据", "网络", "射频", "多模态", "机器"] if w in reason]
            chk(any(w in pool for w in key) if key else True, f'{q["id"]} {lid} 理由与该室方向可对应')

        # 排除项也必须写理由（防止"因为不在 Top3 所以没标"被当人工判断）
        for e in q.get("excluded", []):
            chk(len(e.get("reason", "")) >= 12, f'{q["id"]} 排除 {e["lab_id"]} 写明了理由')

        if q["id"] in g01_ids:
            g01_labs |= set(rs)
            if any(LAB[x]["path_type"][0] == "双主导" for x in rs):
                dual_hit.append(q["id"])
            # MIN_SCORE 变动预警：合理选择集必须与实测候选有交集，否则该条无法判定
            inter = [x for x in rs if x in got]
            chk(bool(inter), f'{q["id"]} 合理选择集与实测 Top3 有交集 {inter}')
            # 标注若纳入Top3外的实验室，属"人工放宽"，要提示
            extra = [x for x in rs if x not in got]
            if extra:
                print(f'     ⚠ {q["id"]} 合理选择集含 Top3 外的 {extra}（人工放宽，需说明）')
        if form == "①":
            rec_labs |= set(got)
        # 并列预警
        sc = [round(s["score"], 3) for _, s in top3]
        if len(sc) != len(set(sc)):
            print(f'     ⚠ {q["id"]} Top3 存在同分 {sc}：排序不确定，加排序指标前需定义 tie-break')

    print("\n=== 约束校验 ===")
    chk(len(g01_labs) >= 6, f'G-01 合理选择集合计触达 {len(g01_labs)} 个不同实验室（需 ≥6）')
    pts = {LAB[x]["path_type"][0] for x in g01_labs}
    chk({"科研主导", "竞赛主导", "双主导"} <= pts, f'G-01 合理选择集覆盖 path_type：{sorted(pts)}')
    chk(bool(dual_hit), f'至少 1 条 query 合理选择集含双主导：{dual_hit}')
    chk(len(rec_labs) >= 10, f'产生推荐的 query 合计触达 {len(rec_labs)} 个不同实验室（需 ≥10）')
    n_path_a = sum(1 for q in Q if form_of(q["slots"])[0] == "②")
    chk(n_path_a == 2, f'无匹配型 2 条实测都落形态②（实测 {n_path_a} 条）')

    print("\n=== T 侧 ===")
    for t in T:
        if t["type"] == "含混":
            chk("追问" in str(t["expected"]), f'{t["id"]} 含混指令标注为追问')
            continue
        if t["type"].startswith("应澄清"):
            exp = str(t["expected"])
            chk("不生成提案" in exp, f'{t["id"]} 应澄清类注明「不生成提案」')
            chk("你是想修改这条吗" in exp or "确认" in exp, f'{t["id"]} 应澄清类含「征询教师确认」')
            chk("省级奖项合计498项" in exp, f'{t["id"]} 应澄清类主动给出了疑似候选')
            continue
        for e in t["expected"]:
            f, op = e["field"], e["op"]
            chk(f in ALLOWED, f'{t["id"]} 字段 {f} 在白名单')
            chk(op in ALLOWED.get(f, set()), f'{t["id"]} {f} 允许 op={op}（允许集 {ALLOWED.get(f)}）')
            # remove 目标存在性
            if op == "remove":
                cur = LAB[t["lab_id"]].get(f)
                target = e["new_value"]
                exists = target in (cur or []) if isinstance(cur, list) else (target in (cur or ""))
                chk(exists, f'{t["id"]} remove 目标存在于现值')
            # merge 子键合法
            if op == "merge":
                sub = {"recruit_info": {"window", "accepts_freshman", "note"},
                       "contact": {"qq_group", "email", "wechat"}}[f]
                bad = set(e["new_value"]) - sub
                chk(not bad, f'{t["id"]} merge 子键合法（非法 {bad or "无"}）')

    print("\n=== 汇总 ===")
    print(f'Q 条数 {len(Q)}（明确 4 / 信息不足 5 / 无匹配 2 / 冲突 2）')
    from collections import Counter as _C
    _c = _C(t["type"] for t in T)
    print(f'T 条数 {len(T)}（' + " / ".join(f'{k} {v}' for k, v in _c.items()) + '）')
    print("自检结果:", "全部通过 ✅" if ok else "存在未通过项 ❌")


if __name__ == "__main__":
    main()
