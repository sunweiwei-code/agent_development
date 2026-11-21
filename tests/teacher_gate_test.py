# -*- coding: utf-8 -*-
"""
闸门反测试：证明四道闸门不是摆设。

指标（G-04a/b/G-05）只测"正常流程对不对"，测不出"该拦的有没有拦"。
这里的每个用例都构造一个**本该被拒绝**的提案，断言它确实被拦。

用法： python tests/teacher_gate_test.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import teacher  # noqa: E402


def case(name, fn, expect_prefix):
    try:
        got = fn()
    except Exception as e:
        got = f"EXC {type(e).__name__}: {e}"
    ok = str(got or "").startswith(expect_prefix)
    print(f'  {"✅" if ok else "❌"} {name:34s} 期望 {expect_prefix:18s} 实得 {str(got)[:46]}')
    return ok


def main():
    labs = teacher.load_labs()
    l01 = next(l for l in labs if l["lab_id"] == "L01")
    ok = 0
    total = 0

    def run(label, fn, expect):
        nonlocal ok, total
        total += 1
        ok += case(label, fn, expect)

    # 闸门① 字段白名单
    run("改 name（未授权字段）",
        lambda: teacher.gate1([{"field": "name", "op": "replace", "new_value": "X"}]),
        "field_not_allowed")

    # 闸门① op 与类型不匹配：对象字段用 replace
    run("recruit_info 用 replace",
        lambda: teacher.gate1([{"field": "recruit_info", "op": "replace",
                                "new_value": {"accepts_freshman": "yes"}}]),
        "proposal_invalid")

    # 闸门① 对象子键非法
    run("contact 子键非法（phone）",
        lambda: teacher.gate1([{"field": "contact", "op": "merge",
                                "new_value": {"phone": "123"}}]),
        "proposal_invalid")

    # 闸门② lab 隔离
    run("跨实验室写入",
        lambda: teacher.gate_gate2(
            [{"lab_id": "L02", "field": "intro", "op": "replace", "new_value": "x"}], "L01"),
        "lab_mismatch")

    # 闸门③ 层1 基线过期
    run("基线过期（old_value 被改）",
        lambda: teacher.gate3(
            {"field": "location", "op": "replace", "old_value": "旧地址",
             "new_value": "C1-301"}, l01, l01.get("location")),
        "stale_value")

    # 闸门③ 层2 重复追加
    exist = (l01.get("achievements") or [""])[0]
    run("追加已存在的成果",
        lambda: teacher.gate3(
            {"field": "achievements", "op": "append", "old_value": l01.get("achievements"),
             "new_value": exist}, l01, l01.get("achievements")),
        "dup_append")

    # 闸门③ 层2 删除不存在项
    run("删除不存在的方向",
        lambda: teacher.gate3(
            {"field": "research_areas", "op": "remove",
             "old_value": l01.get("research_areas"), "new_value": "量子引力"},
            l01, l01.get("research_areas")),
        "value_not_found")

    # 闸门④ 未确认不落库
    def unconfirmed():
        before = teacher.LABS.read_bytes()
        r = teacher.apply("实验室地点搬到 C1-999 了", "L01", confirmed=False)
        after = teacher.LABS.read_bytes()
        if before != after:
            return "写入发生了（闸门④失效）"
        return r.get("code")
    run("未确认直接写入", unconfirmed, "not_confirmed")

    # 合并语义：merge 不得清空未提及子键
    def merge_keeps():
        l = dict(l01)
        l["recruit_info"] = {"window": "9月", "accepts_freshman": "yes", "note": "需面试"}
        new = teacher.apply_value(l["recruit_info"], "merge", {"accepts_freshman": "senior_only"})
        return "OK" if (new.get("window") == "9月" and new.get("note") == "需面试") else "子键被清空"
    run("merge 保留未提及子键", merge_keeps, "OK")

    # 快照是一次性的：回滚成功即消费，列表里不该再留着
    def rollback_consumes():
        sid = teacher.snapshot("L01")
        d = teacher.SNAPS / sid
        if not d.exists():
            return "快照没建出来"
        r = teacher.rollback(sid)
        if not r.get("identical"):
            return "回滚未逐字一致"
        if d.exists():
            return "快照仍在（未消费，列表会残留）"
        return "OK"
    run("回滚后快照被消费", rollback_consumes, "OK")

    # 相对时间必须落到具体年份，不能让「今年」原样进档案
    def rel_year():
        y = datetime.now().year
        got = teacher.resolve_time("获得今年挑战杯金奖")
        return "OK" if got == f"获得{y}年挑战杯金奖" else got
    run("相对时间归一「今年」", rel_year, "OK")

    # 数量汇总：「总共获得 100000 个奖」是统计陈述，不是编造，不能被当成"内容不足"
    def agg_claim():
        got = teacher.aggregate_item("我们总共获得了100000个奖")
        return "OK" if got == "累计获得各类奖项 100000 项" else str(got)
    run("数量汇总句归一", agg_claim, "OK")

    # 反过来：说了具体赛事/等级，就不是汇总，不能被塞成"累计 N 项"
    def agg_not_detail():
        got = teacher.aggregate_item("获得今年挑战杯金奖")
        return "OK" if got is None else f"误判为汇总：{got}"
    run("具体奖项不算汇总", agg_not_detail, "OK")

    # 模型追问时的兜底：必须仍能产出一条可确认的 append
    def agg_fallback():
        l04 = next(l for l in labs if l["lab_id"] == "L04")
        got = teacher._aggregate_fallback("我们总共获得了100000个奖", l04, "L04", "teacher")
        if not got:
            return "兜底没产出提案（会退化成一味追问）"
        p = got[0]
        return "OK" if p["field"] == "achievements" and p["op"] == "append" else str(p)
    run("汇总句兜底提案", agg_fallback, "OK")

    # 汇总只该有一条：库里已有 100000，报 120000 应变成「删旧 + 加新」
    def agg_single():
        lab = {"achievements": ["某省一等奖", "累计获得各类奖项 100000 项"]}
        props = teacher._dedupe_aggregate(
            [{"field": "achievements", "op": "append",
              "new_value": "累计获得各类奖项 120000 项"}], lab)
        ops = [p["op"] for p in props]
        return "OK" if ops == ["remove", "append"] else f"得到 {ops}（会出现两条汇总）"
    run("汇总条目只留一条", agg_single, "OK")

    # 数字没变就别硬造一次写入
    def agg_same():
        lab = {"achievements": ["累计获得各类奖项 100000 项"]}
        props = teacher._dedupe_aggregate(
            [{"field": "achievements", "op": "append",
              "new_value": "累计获得各类奖项 100000 项"}], lab)
        return "OK" if not props else f"仍有 {len(props)} 条提案"
    run("汇总数字未变不写入", agg_same, "OK")

    # 中文数量换算：「一万个奖」也要能判出量级
    def cn_num():
        cases = {"三十": 30, "一万": 10000, "一百二十": 120, "250": 250}
        bad = {k: v for k, v in cases.items() if teacher.cn2int(k) != v}
        return "OK" if not bad else f"算错 {bad}"
    run("中文数量换算", cn_num, "OK")

    # 追问的口径：100000 该问一句，30 不该（正常规模被盘问就是噪音）
    def suspect():
        big = teacher.agg_suspect("累计获得各类奖项 100000 项", 5)
        small = teacher.agg_suspect("累计获得各类奖项 30 项", 5)
        cn = teacher.agg_suspect("累计获得各类奖项 一万 项", 5)
        return "OK" if (big and cn and not small) else \
            f"100000={big} 一万={cn} 30={small}（30 应为 False）"
    run("只对离谱数量追问", suspect, "OK")

    print(f"\n闸门反测试：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
