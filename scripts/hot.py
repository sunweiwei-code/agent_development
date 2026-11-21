# -*- coding: utf-8 -*-
"""
首页气泡：今天该飘哪 12 个实验室（V3.63）。

凭什么排前 12：**被咨询的次数**。两个来源都算一次咨询——
  · 一轮对话里进入 top3（学生问到了它）
  · 学生主动点开它的档案页（`/api/lab/<id>?hit=1`）
老师改档案不走这两条路，所以不会被算进去：那是编辑，不是咨询。

为什么要有"每日刷新"的兜底：开学头几天没人问过任何一个实验室，
计数全是 0，照实排就是空的；随机凑满 12 个、按日期做种子，
同一天刷新页面看到的是同一组（不会每次刷新都换一批，那会让人以为页面在抽风），
隔天再来才换。计数一旦长起来，真热门会自然把随机位挤掉。

存哪儿：`data/labs/hot.json`，一个 `{lab_id: 次数}`。运行时数据，不进版本库。
读写整文件替换 + 加锁：ThreadingHTTPServer 下两条请求可能同时加计数。
"""
import json
import os
import random
import re
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "data" / "labs" / "hot.json"
LABS = ROOT / "data" / "labs" / "labs.json"

TOP_N = 12          # 首页一圈气泡的数量；实验室表只有 45 个，留够余量
LAB_ID_RE = re.compile(r"^L\d{1,3}$")
_LOCK = threading.Lock()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _read():
    try:
        d = json.loads(STORE.read_text(encoding="utf-8"))
    except Exception:
        return {"counts": {}, "updated": None}
    if not isinstance(d, dict) or not isinstance(d.get("counts"), dict):
        return {"counts": {}, "updated": None}
    d.setdefault("updated", None)
    return d


def _write(d):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STORE)


def hit(lab_ids, weight=1):
    """记一次咨询。返回写入后的 counts；坏 id 直接丢掉，不让脏键把文件撑大。"""
    ids = [str(x) for x in (lab_ids or []) if x and LAB_ID_RE.match(str(x))]
    if not ids:
        return {}
    with _LOCK:
        d = _read()
        c = d["counts"]
        for lid in ids:
            c[lid] = int(c.get(lid) or 0) + weight
        d["updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        try:
            _write(d)
        except OSError as e:
            print(f"[hot] 写入失败：{e}")
        return dict(c)


def counts():
    return dict(_read().get("counts") or {})


def top(n=TOP_N):
    """今天首页要飘的气泡：真热门在前，不够就用当天随机的一组补齐。"""
    try:
        labs = json.loads(LABS.read_text(encoding="utf-8"))
    except Exception:
        labs = []
    valid = {l["lab_id"]: l for l in labs if l.get("lab_id")}
    c = counts()

    # 计数降序；同分按 id 稳定排序，免得两个 1 次的实验室每次刷新互换位置
    ranked = sorted(valid, key=lambda k: (-int(c.get(k) or 0), k))
    hot = [k for k in ranked if int(c.get(k) or 0) > 0][:n]

    picked = [{"lab_id": k, "name": valid[k].get("name") or k,
               "count": int(c.get(k) or 0), "source": "hot"} for k in hot]

    if len(picked) < n:
        rest = [k for k in valid if k not in hot]
        # 日期做种子：同一天怎么刷都是这批，隔天才换
        random.Random(f"{_today()}|{len(valid)}").shuffle(rest)
        for k in rest[:n - len(picked)]:
            picked.append({"lab_id": k, "name": valid[k].get("name") or k,
                           "count": 0, "source": "random"})

    for i, p in enumerate(picked, 1):
        p["rank"] = i
    return {"date": _today(), "total": len(valid),
            "hot": len(hot), "labs": picked}
