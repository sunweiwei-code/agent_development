# -*- coding: utf-8 -*-
"""
用冻结评测集跑学生链路，产出 G-01 / G-01b / G-03 首版数字。

判定口径（PRD 1.2）：
  G-01   Top3 中至少 1 个被标注者判为「合理」的 query 占比；分母 = Q-01~04 + Q-12~13 共 6 条；目标 ≥5/6
  G-01b  无匹配型（Q-10~11）是否明确声明无匹配而不硬凑；目标 2/2
  G-03   信息不足型（Q-05~09）是否追问而非硬凑；目标 5/5

用法：
    python scripts/run_eval.py            # 全量
    python scripts/run_eval.py --limit 3  # 只跑前 3 条（调试）
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import recommend
import re
from reasons import ORG_RE, load_whitelist, verify_names

ROOT = Path(__file__).resolve().parents[1]
Q = json.loads((ROOT / "data/eval/queries.json").read_text(encoding="utf-8"))

G01_IDS = {"Q-01", "Q-02", "Q-03", "Q-04", "Q-12", "Q-13"}
NOMATCH_IDS = {"Q-10", "Q-11"}
INSUFFICIENT_IDS = {"Q-05", "Q-06", "Q-07", "Q-08", "Q-09"}


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    qs = Q[:limit] if limit else Q

    results = []
    t0 = time.time()
    for q in qs:
        r = recommend(q["utterance"])
        reasonable = [e["lab_id"] for e in q.get("reasonable_set", [])]
        top3 = [t["lab_id"] for t in r["top3"]]
        hit = [x for x in top3 if x in reasonable]
        results.append({"q": q, "r": r, "reasonable": reasonable, "top3": top3, "hit": hit})
        flag = "追问" if r["form"] == "追问" else f'形态{r["form"]}'
        print(f'{q["id"]} {flag:6s} Top3={top3 or "—"}  合理集={reasonable or "—"}'
              f'{"  ✅" if hit else ""}')

    print(f"\n耗时 {time.time()-t0:.1f}s")

    # ---- G-01 ----
    g01 = [x for x in results if x["q"]["id"] in G01_IDS]
    g01_pass = [x for x in g01 if x["hit"]]
    print(f"\n=== G-01（Top3 含合理选择）===")
    print(f"  {len(g01_pass)}/{len(g01)} 条通过（目标 ≥5/6）")
    for x in g01:
        if not x["hit"]:
            print(f'  ❌ {x["q"]["id"]} Top3={x["top3"]} 但合理集={x["reasonable"]}')

    # ---- G-01b ----
    nm = [x for x in results if x["q"]["id"] in NOMATCH_IDS]
    nm_pass = [x for x in nm if x["r"]["form"] == "②" and not x["r"]["others"]]
    print(f"\n=== G-01b（无匹配不硬凑）===")
    print(f"  {len(nm_pass)}/{len(nm)} 条通过")
    for x in nm:
        if x["r"]["form"] != "②":
            print(f'  ❌ {x["q"]["id"]} 形态={x["r"]["form"]} others={[o["lab_id"] for o in x["r"]["others"]]}')

    # ---- G-03 ----
    # 判据用标注的 expected_form（"③/①" 表示两者都可），不额外加严
    ins = [x for x in results if x["q"]["id"] in INSUFFICIENT_IDS]
    def form_ok(x):
        return x["r"]["form"] in [f.strip() for f in x["q"]["expected_form"].split("/")]
    ins_pass = [x for x in ins if form_ok(x)]
    print(f"\n=== G-03（信息不足→正确形态，不硬凑）===")
    print(f"  {len(ins_pass)}/{len(ins)} 条通过")
    for x in ins:
        if not form_ok(x):
            print(f'  ❌ {x["q"]["id"]} 形态={x["r"]["form"]} 期望={x["q"]["expected_form"]} Top3={x["top3"]}')

    # ---- G-02 名称幻觉 ----
    # 严格按 PRD 口径：分母 = **包含实验室名称的输出次数**（不是"所有输出"）。
    # 输出范围：Top3 名称、others 名称、推荐理由文本。
    wl = load_whitelist()
    wl_norm = {re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", v) for v in wl.values()}
    bad_names, swap = [], []
    total_outputs = 0          # 所有待检输出
    named_outputs = 0          # 其中"包含实验室名称"的
    for x in results:
        r = x["r"]
        texts = [("名称", t["name"]) for t in r["top3"]]
        texts += [("名称", o["name"]) for o in r.get("others", [])]
        texts += [("理由", t.get("reason_text", "")) for t in r["top3"]]
        for kind, txt in texts:
            if not txt:
                continue
            total_outputs += 1
            names_here = ORG_RE.findall(txt)
            if names_here:
                named_outputs += 1
            for b in verify_names(txt, wl):
                bad_names.append((x["q"]["id"], kind, b))
        for t in r["top3"]:
            for other in r["top3"]:
                if other["lab_id"] != t["lab_id"] and other["name"] in t.get("reason_text", ""):
                    swap.append((x["q"]["id"], t["lab_id"], other["name"]))

    print(f"\n=== G-02（名称幻觉率，需为 0）===")
    print(f"  待检输出 {total_outputs} 次，其中**含实验室名称**的 {named_outputs} 次（= 分母）")
    print(f"  幻觉名称 {len(bad_names)} 个（= 分子）")
    rate = f"{len(bad_names)/named_outputs:.1%}" if named_outputs else "—"
    print(f"  **幻觉率 {rate}**（目标 0.00%）")
    for b in bad_names[:5]:
        print(f'  ❌ {b[0]} {b[1]} 出现库外名：{b[2]}')
    if swap:
        print(f"  ⚠ 张冠李戴 {len(swap)} 处：")
        for s_ in swap[:5]:
            print(f'     {s_[0]} {s_[1]} 的理由里出现了 {s_[2]}')

    # ---- 分支覆盖（多轮/非常见形态；不计入 G 指标分母）----
    BC = json.loads((ROOT / "data/eval/branch_cases.json").read_text(encoding="utf-8"))
    print(f"\n=== 分支覆盖（3 条，不计入 G 指标）===")
    from pipeline import recommend as _rec
    for c in BC:
        hist, last = None, None
        for turn in c["turns"]:
            last = _rec(turn, history=hist)
            hist = last.get("history")
        got = last["form"]
        ok = got == c["expected_form"]
        extra = ""
        if ok and c.get("expect_top1_path"):
            p1 = (last["top3"][0]["path_type"] if last["top3"] else None)
            ok = (p1 == c["expect_top1_path"])
            extra = f'  top1路径={p1}' +(f'（期望 {c["expect_top1_path"]}）' if not ok else "")
        print(f'  {"✅" if ok else "❌"} {c["id"]} {c["name"]}：期望{c["expected_form"]} 实得{got}'
              f'  top3={[t["lab_id"] for t in last["top3"]]} others={[o["lab_id"] for o in last.get("others", [])]}{extra}')
        if not ok:
            print(f'     说明：{c["why"][:80]}')

    # ---- 内容一致性（观察项，非 G 指标）----
    dwar = [(x["q"]["id"], t["lab_id"], t.get("dir_warnings", []))
            for x in results for t in x["r"]["top3"] if t.get("dir_warnings")]
    print(f"\n=== 内容一致性（理由提到该室没有的方向）===")
    print(f"  命中 {len(dwar)} 处（软标记，不阻断）")
    for d_ in dwar[:5]:
        print(f'  ⚠ {d_[0]} {d_[1]}: {d_[2]}')

    # ---- 抽取质量（附带观察，不计入指标）----
    print(f"\n=== 槽位抽取 vs 标注（观察项）===")
    for x in results:
        got = x["r"]["slots"]
        exp = x["q"].get("slots") or {}
        diffs = []
        for k in ("interest", "goal", "grade"):
            g, e = got.get(k), exp.get(k)
            if (g or None) != (e or None):
                diffs.append(f'{k}: 抽「{g}」vs 标「{e}」')
        if diffs:
            print(f'  {x["q"]["id"]} ' + "；".join(diffs))

    out = ROOT / "data/eval/eval_run.json"
    out.write_text(json.dumps([{
        "id": x["q"]["id"], "form": x["r"]["form"], "top3": x["top3"],
        "reasonable": x["reasonable"], "hit": x["hit"],
        "slots": x["r"]["slots"], "expected_slots": x["q"].get("slots"),
        "reasons": [{"lab_id": t["lab_id"], "text": t.get("reason_text", ""),
                     "fallback": t.get("reason_fallback", True)} for t in x["r"]["top3"]],
    } for x in results], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存 {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
