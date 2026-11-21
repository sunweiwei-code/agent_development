# -*- coding: utf-8 -*-
"""
学生端跨会话长期记忆（V3.56）。

存哪儿：`data/students/<sid>.json`，一学生一文件，机器本地，不出网。
为什么不是 localStorage：学生真的会在机房、宿舍、手机各开一次，换台设备就
"你是谁我不认识"；而服务端一个文件就能接着上次说。代价是要处理个人信息，
所以这里做了三条硬约束：

  1. **只存够用来续上的东西**：6 个槽位 + 最近 12 轮的短问句（≤60 字）+
     命中的 lab_id。不存姓名、学号、院系、IP、UA、完整对话。
  2. **身份只是一个随机串**：`s-` + 16 位十六进制，和学生身份没有任何映射，
     拿不到这个串就打不开别人的记忆（不提供按名字查询的接口）。
  3. **可清除、会过期**：学生点「清除」即删文件；90 天没动过自动删除。

读写都是整文件替换（tmp + os.replace），并加锁——ThreadingHTTPServer 下
同一学生的两条请求可能并发。
"""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "data" / "students"

TTL_DAYS = 90
MAX_TURNS = 12          # 只留最近 12 轮，够回答"上次聊到哪"
SESSION_GAP = 1800      # 间隔超过 30 分钟算新的一次访问
CLIP_Q = 60             # 问句只留前 60 字
CLIP_V = 40             # 槽位值只留前 40 字

# 只认这 6 个槽位，别的一律不落盘（防止上游多塞字段把文件撑大）
SLOT_KEYS = ("interest", "goal", "tech_stack", "grade", "preferred_teacher", "target_contest")
GOAL_CN = {"科研": "想走科研", "竞赛": "想打比赛", "就业": "想就业"}

SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
_LOCK = threading.Lock()
_last_sweep = 0.0


# ---------------- 身份 ----------------
def new_sid():
    return "s-" + secrets.token_hex(8)


def valid_sid(s):
    return bool(s) and bool(SID_RE.match(str(s)))


def _path(sid):
    return STORE / f"{sid}.json"


# ---------------- 时间 ----------------
def _stamp():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _age_seconds(ts):
    try:
        return (datetime.now() - datetime.fromisoformat(str(ts))).total_seconds()
    except Exception:
        return 10 ** 9


def _clip(v, n):
    s = v if isinstance(v, str) else str(v)
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


# ---------------- 读写 ----------------
def load(sid):
    """读一个学生的记忆；不存在、损坏、过期都返回 None（过期顺手删掉）。"""
    if not valid_sid(sid):
        return None
    p = _path(sid)
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime > TTL_DAYS * 86400:
            p.unlink()
            return None
        mem = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(mem, dict):
        return None
    mem.setdefault("sid", sid)
    mem.setdefault("slots", {})
    mem.setdefault("recent", [])
    mem.setdefault("visits", 1)
    mem.setdefault("turns", len(mem["recent"]))
    return mem


def _write(sid, mem):
    STORE.mkdir(parents=True, exist_ok=True)
    p = _path(sid)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(mem, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def clear(sid):
    """删干净，不留墓碑——学生点清除就该是真的没有了。"""
    if not valid_sid(sid):
        return False
    p = _path(sid)
    try:
        if p.exists():
            p.unlink()
            return True
    except OSError:
        pass
    return False


def maybe_sweep(hours=1.0):
    """按小时节流地扫一遍过期文件。"""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < hours * 3600:
        return 0
    _last_sweep = now
    n = 0
    if not STORE.exists():
        return 0
    for p in STORE.glob("*.json"):
        try:
            if now - p.stat().st_mtime > TTL_DAYS * 86400:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


# ---------------- 写入：一轮说完之后 ----------------
def observe(sid, text, result):
    """把这一轮里"值得记住的"并进记忆。返回写入后的 mem（失败返回 None）。"""
    if not valid_sid(sid) or not isinstance(result, dict):
        return None
    with _LOCK:
        mem = load(sid) or {"sid": sid, "created": _stamp(), "visits": 0,
                            "turns": 0, "slots": {}, "recent": [], "last_lab": None}
        # 隔了半小时以上再开口 = 又来了
        if not mem.get("last_seen") or _age_seconds(mem["last_seen"]) > SESSION_GAP:
            mem["visits"] = int(mem.get("visits") or 0) + 1

        # 槽位：非空才覆盖。空值表示"这轮没说"，不是"要忘掉"；
        # "未定"是"没说清"，存下来只会让下次画像多一行噪音
        for k, v in (result.get("slots") or {}).items():
            if k in SLOT_KEYS and v and v != "未定":
                mem["slots"][k] = _clip(v, CLIP_V)

        h = result.get("history") or {}
        ids = [x for x in (h.get("labs") or []) if x]
        if h.get("last_lab"):
            mem["last_lab"] = h["last_lab"]
        elif ids:
            mem["last_lab"] = ids[0]

        mem["recent"].append({"q": _clip(text, CLIP_Q),
                              "form": result.get("form") or "",
                              "labs": ids[:3], "ts": _stamp()})
        mem["recent"] = mem["recent"][-MAX_TURNS:]
        mem["turns"] = int(mem.get("turns") or 0) + 1
        mem["last_seen"] = _stamp()
        try:
            _write(sid, mem)
        except OSError as e:
            print(f"[memory] 写入失败：{e}")
            return mem
        return mem


# ---------------- 读出：这一轮开始之前 ----------------
def recent_labs(mem, want=3):
    """最近几轮命中过的 lab_id（倒序取，去重）。

    只取最后一轮是不够的——上一轮可能是一次"详情"，没有候选列表，
    这时"它/这个实验室"就没得指代。
    """
    out = []
    for r in reversed((mem or {}).get("recent") or []):
        for lid in r.get("labs") or []:
            if lid not in out:
                out.append(lid)
        if len(out) >= want:
            break
    return out[:want]


def to_history(mem):
    """记忆 → pipeline 认的 history。

    asked 固定归零：新的一次访问应该重新获得"追问一次"的机会，
    否则上次问过了，这次张口就被兜底，永远不会再问。
    """
    if not mem:
        return {"slots": {}, "asked": 0}
    return {"slots": dict(mem.get("slots") or {}), "asked": 0,
            "labs": recent_labs(mem), "last_lab": mem.get("last_lab")}


def merge_history(mem, client):
    """记忆与页面内 history 合并。

    页面内的更可信（是这次亲口说的），但**按槽位逐键覆盖**，不是整块替换：
    整块替换会把"这次只说了'随便看看'"的学生退回空白画像，记忆等于白存。
    """
    client = client if isinstance(client, dict) else {}
    if not mem:
        return client or {"slots": {}, "asked": 0}
    mine = to_history(mem)
    out = dict(client) if client else dict(mine)
    slots = dict(mine.get("slots") or {})
    for k, v in (client.get("slots") or {}).items():
        if v:
            slots[k] = v
    out["slots"] = slots
    out["asked"] = int(client.get("asked") or 0)
    out["labs"] = list(client.get("labs") or []) or list(mine.get("labs") or [])
    out["last_lab"] = client.get("last_lab") or mine.get("last_lab")
    return out


# ---------------- 给前端 ----------------
def profile(mem, keys=None):
    """一句话画像：只列学生自己说过的，不推断。

    keys 用于"这一轮到底是哪几条记忆在起作用"——只报真正用上的，
    别把没参与本次判断的槽位也挂上去。
    """
    s = (mem or {}).get("slots") or {}
    if keys is not None:
        s = {k: v for k, v in s.items() if k in keys}
    bits = [s.get("interest"), s.get("grade"), GOAL_CN.get(s.get("goal")),
            s.get("tech_stack")]
    return " · ".join(x for x in bits if x)[:80]


def public(mem):
    if not mem:
        return {"exists": False}
    return {"exists": True,
            "turns": int(mem.get("turns") or 0),
            "visits": int(mem.get("visits") or 1),
            "profile": profile(mem),
            "slots": {k: v for k, v in (mem.get("slots") or {}).items() if k in SLOT_KEYS},
            "last_lab": mem.get("last_lab"),
            "updated": (mem.get("last_seen") or "")[:16].replace("T", " "),
            "recent": [r.get("q") for r in (mem.get("recent") or [])[-3:] if r.get("q")]}
