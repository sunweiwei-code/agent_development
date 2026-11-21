# -*- coding: utf-8 -*-
"""
LangSmith 同步（M1 验收证据）：建 Dataset → 写 Examples → 跑 Experiment 留痕。

为什么不用 SDK：本机 pip 装不上（150s 超时无输出，langsmith/chromadb 都一样），
所以用标准库 urllib 直连 REST API。代价是要自己处理 dotted_order，收益是不依赖环境。

用法：
    python scripts/langsmith_sync.py --dataset        # 建/更新 Dataset 与 Examples
    python scripts/langsmith_sync.py --experiment     # 跑一次 Experiment 并上报
    python scripts/langsmith_sync.py --check          # 只查连通与统计
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DS_NAME = "lab-reco-eval-v1"


def _cfg():
    cfg = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


CFG = _cfg()
EP = CFG.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")
KEY = CFG["LANGSMITH_API_KEY"]


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        f"{EP}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-api-key": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def ensure_dataset():
    s, ds = api(f"/api/v1/datasets?name={DS_NAME}")
    for d in (ds or []):
        if d.get("name") == DS_NAME:
            print(f"Dataset 已存在：{DS_NAME} (id={d['id']})")
            return d["id"]
    s, d = api("/api/v1/datasets", "POST", {
        "name": DS_NAME,
        "description": "实验室推荐 Agent 学生侧评测集（13 条，来自冻结版 queries.json）",
        "data_type": "kv",
    })
    if s not in (200, 201):
        raise SystemExit(f"建 Dataset 失败：{s} {d}")
    print(f"Dataset 已创建：{DS_NAME} (id={d['id']})")
    return d["id"]


def example_id(qid):
    """确定性 ID：重复写入不会产生重复 example。"""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"lab-reco/{DS_NAME}/{qid}"))


def sync_examples(ds_id):
    Q = json.loads((ROOT / "data/eval/queries.json").read_text(encoding="utf-8"))
    rows = []
    for q in Q:
        rows.append({
            "id": example_id(q["id"]),
            "dataset_id": ds_id,
            "inputs": {"utterance": q["utterance"], "query_id": q["id"]},
            "outputs": {
                "reasonable_set": [e["lab_id"] for e in q.get("reasonable_set", [])],
                "expected_form": q["expected_form"],
                "type": q["type"],
            },
        })
    s, r = api("/api/v1/examples/bulk", "POST", rows)
    if s not in (200, 201):
        # 已有则跳过
        print(f"[warn] 批量写入返回 {s}（多为已存在）：{str(r)[:160]}")
    else:
        print(f"写入 {len(rows)} 条 Examples")
    s, cnt = api(f"/api/v1/examples?dataset_id={ds_id}&limit=100")
    n = len(cnt) if isinstance(cnt, list) else "?"
    print(f"当前 Dataset 内 Examples：{n}")
    return {q["id"]: example_id(q["id"]) for q in Q}


def ensure_experiment(ds_id, name):
    """创建真正挂在 Dataset 下的 Experiment。

    坑：只在 run 上写 session_name 会得到一个普通 **project**，
    界面上不会出现在 Datasets → Experiments 里。必须显式建 session 并带
    reference_dataset_id，runs 再用 session_id 关联。
    """
    s, d = api("/api/v1/sessions?limit=50")
    for x in (d or []):
        if x.get("name") == name:
            return x["id"]
    s, d = api("/api/v1/sessions", "POST", {
        "name": name,
        "reference_dataset_id": ds_id,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "description": "M1 学生链路跑分",
    })
    if s not in (200, 201):
        raise SystemExit(f"建 Experiment 失败：{s} {str(d)[:200]}")
    return d["id"]


def run_experiment(exp_name=None, limit=None):
    from run_eval import G01_IDS, INSUFFICIENT_IDS, NOMATCH_IDS
    from pipeline import recommend

    ds_id = ensure_dataset()
    ex_ids = sync_examples(ds_id)
    exp_name = exp_name or f"student-chain-{datetime.now().strftime('%Y%m%d-%H%M')}"
    exp_id = ensure_experiment(ds_id, exp_name)

    Q = json.loads((ROOT / "data/eval/queries.json").read_text(encoding="utf-8"))
    if limit:
        Q = Q[:limit]

    print(f"\nExperiment: {exp_name}（{len(Q)} 条）")
    rows, stats = [], {"g01": 0, "g01n": 0, "g01b": 0, "g01bn": 0, "g03": 0, "g03n": 0, "named": 0, "halluc": 0}
    for q in Q:
        t0 = datetime.now(timezone.utc)
        r = recommend(q["utterance"])
        t1 = datetime.now(timezone.utc)
        reasonable = [e["lab_id"] for e in q.get("reasonable_set", [])]
        top3 = [t["lab_id"] for t in r["top3"]]
        hit = [x for x in top3 if x in reasonable]
        outputs = {"form": r["form"], "top3": top3,
                   "reasons": {t["lab_id"]: t.get("reason_text", "") for t in r["top3"]}}
        scores = {}
        if q["id"] in G01_IDS:
            stats["g01n"] += 1
            scores["g01_hit"] = 1 if hit else 0
            stats["g01"] += scores["g01_hit"]
        if q["id"] in NOMATCH_IDS:
            stats["g01bn"] += 1
            scores["g01b_ok"] = 1 if (r["form"] == "②" and not r["others"]) else 0
            stats["g01b"] += scores["g01b_ok"]
        if q["id"] in INSUFFICIENT_IDS:
            stats["g03n"] += 1
            ok = r["form"] in [f.strip() for f in q["expected_form"].split("/")]
            scores["g03_ok"] = 1 if ok else 0
            stats["g03"] += scores["g03_ok"]

        rid = str(uuid.uuid4())
        ts0 = t0.strftime("%Y%m%dT%H%M%S%fZ")
        rows.append({
            "id": rid, "trace_id": rid, "parent_run_id": None,
            "name": "student_chain", "run_type": "chain",
            "start_time": t0.isoformat(), "end_time": t1.isoformat(),
            "inputs": {"utterance": q["utterance"], "query_id": q["id"]},
            "outputs": outputs,
            "extra": {"metadata": {"form": r["form"], "slots": r["slots"],
                                   "scores": scores, "query_type": q["type"]}},
            "session_id": exp_id,
            "session_name": exp_name,
            "reference_example_id": ex_ids.get(q["id"]),
            "dotted_order": f"{ts0}{rid}",
        })
        flag = "✅" if (not scores or all(scores.values())) else "  "
        print(f'  {flag} {q["id"]} 形态{r["form"]:4s} Top3={top3 or "—"} {scores or ""}')

    # /runs/batch 要的是对象（{"post": [...]}），直接传数组会 422
    s, resp = api("/api/v1/runs/batch", "POST", {"post": rows})
    print(f"\n上报 {len(rows)} 个 run → HTTP {s}")
    if s not in (200, 201, 202):
        print("  响应:", str(resp)[:300])
        return

    def pct(a, b):
        return f"{a}/{b} = {a/b:.0%}" if b else "—"
    print(f'\n=== {exp_name} 结果 ===')
    print(f'  G-01   {pct(stats["g01"], stats["g01n"])}（目标 ≥5/6）')
    print(f'  G-01b  {pct(stats["g01b"], stats["g01bn"])}')
    print(f'  G-03   {pct(stats["g03"], stats["g03n"])}')
    print(f"\n在 LangSmith 的 Datasets → {DS_NAME} → Experiments 里可查 {exp_name}")


def check():
    s, d = api("/api/v1/sessions?limit=5")
    print("连通:", s, [x.get("name") for x in (d or [])] if isinstance(d, list) else d)
    ds_id = None
    s, ds = api(f"/api/v1/datasets?name={DS_NAME}")
    for x in (ds or []):
        if x.get("name") == DS_NAME:
            ds_id = x["id"]
    if not ds_id:
        print("Dataset 尚未创建")
        return
    s, cnt = api(f"/api/v1/examples?dataset_id={ds_id}&limit=100")
    print(f"Dataset {DS_NAME}: {len(cnt) if isinstance(cnt, list) else '?'} 条 examples")
    s, runs = api(f"/api/v1/runs/query?limit=5&run_type=chain")
    print("最近 chain runs:", len(runs) if isinstance(runs, list) else runs)


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    elif "--dataset" in sys.argv:
        ds = ensure_dataset()
        sync_examples(ds)
    else:
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        run_experiment(limit=lim)
