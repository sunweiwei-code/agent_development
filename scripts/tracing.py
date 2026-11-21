# -*- coding: utf-8 -*-
"""
线上对话的 tracing（LangSmith）。

与 langsmith_sync.py 的区别：
  · sync 是**离线评测**：固定 13 条 → 挂 Dataset 的 Experiment
  · 本模块是**线上交互**：每次对话一个 trace，落到普通 project（lab-reco-live）

三条硬要求：
  ① 异步：上报在后台线程，绝不阻塞学生等回答
  ② 失败静默：LangSmith 挂了不能影响推荐功能（只打日志）
  ③ 有父子结构：根 run = 一轮对话，子 run = 抽取/检索/打分/理由，界面上能看出耗时分布

不用 SDK（pip 装不上），直接 POST /runs/batch，字段手工拼。
"""
import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = "lab-reco-live"

_CFG = None


def _cfg():
    global _CFG
    if _CFG is None:
        c = {}
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip()
        _CFG = c
    return _CFG


def _api(path, body):
    c = _cfg()
    ep = c.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")
    req = urllib.request.Request(
        f"{ep}{path}", method="POST",
        data=json.dumps(body).encode(),
        headers={"x-api-key": c["LANGSMITH_API_KEY"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def _ts(dt):
    return dt.strftime("%Y%m%dT%H%M%S%fZ")


def _run(run_id, trace_id, parent_id, name, rtype, t0, t1, inputs, outputs, dotted, extra=None):
    r = {
        "id": run_id, "trace_id": trace_id, "parent_run_id": parent_id,
        "name": name, "run_type": rtype,
        "start_time": t0.isoformat(), "end_time": t1.isoformat(),
        "inputs": inputs or {}, "outputs": outputs or {},
        "session_name": SESSION, "dotted_order": dotted,
    }
    if extra:
        r["extra"] = {"metadata": extra}
    return r


def log_turn(text, result, steps=None, elapsed=None, history=None):
    """上报一轮对话。返回 None；任何异常只打日志。"""
    try:
        now = datetime.now(timezone.utc)
        root_id = str(uuid.uuid4())
        t0 = now
        root_dotted = f"{_ts(t0)}{root_id}"

        runs = []
        top3 = [t["lab_id"] for t in result.get("top3", [])]
        runs.append(_run(
            root_id, root_id, None, "student_turn", "chain", t0, now,
            {"utterance": text, "asked": (history or {}).get("asked", 0)},
            {"form": result.get("form"), "top3": top3,
             "followup": result.get("followup_question"),
             "notice": result.get("notice") or result.get("no_match_reason"),
             "fallback_basis": result.get("fallback_basis")},
            root_dotted,
            {"elapsed_s": elapsed, "slots": result.get("slots"),
             "missing": result.get("missing")},
        ))

        # 子 run：把 SSE 阶段转成时间轴（stage 名对齐 _done 与起始）
        if steps:
            pend = {}
            for st in steps:
                stage, msg, data = st.get("stage"), st.get("msg"), st.get("data")
                ts = st.get("ts") or now
                base = stage.replace("_done", "")
                if stage.endswith("_done"):
                    start = pend.pop(base, (ts, None))
                    cid = str(uuid.uuid4())
                    runs.append(_run(
                        cid, root_id, root_id, base, "tool",
                        start[0], ts, {"input": start[1]}, {"result": data, "msg": msg},
                        f"{root_dotted}.{_ts(start[0])}{cid}",
                    ))
                else:
                    pend[base] = (ts, msg)
            # 未闭合的阶段按"到现在"结束，避免丢 span
            for base, (st_ts, msg) in pend.items():
                cid = str(uuid.uuid4())
                runs.append(_run(cid, root_id, root_id, base, "tool",
                                 st_ts, now, {"input": msg}, {},
                                 f"{root_dotted}.{_ts(st_ts)}{cid}"))

        _api("/api/v1/runs/batch", {"post": runs})
        return len(runs)
    except Exception as e:                      # ② 失败静默
        print(f"[trace] 上报失败（不影响功能）：{type(e).__name__}: {e}", file=sys.stderr)
        return None


def log_turn_async(*a, **kw):
    """① 异步：放进后台线程。"""
    threading.Thread(target=log_turn, args=a, kwargs=kw, daemon=True).start()


if __name__ == "__main__":
    demo = {"form": "①", "slots": {"interest": "视觉算法"}, "missing": [],
            "top3": [{"lab_id": "L06"}]}
    print("上报结果:", log_turn("自测：我想做视觉算法", demo, elapsed=1.2))
