# -*- coding: utf-8 -*-
"""
教师端写入闭环（M3 核心链）：解析 → 四道闸门 → diff → 快照 → 写入 → 审计 → 回滚。

设计要点（对应 PRD 8.2）：
  闸门① 结构化提案 + 字段白名单
  闸门② lab_id 数据库隔离（越界 = 查不到）
  闸门③ 基线校验（old_value 由系统注入）+ op 操作校验
  闸门④ diff 人工确认 + 写前快照

  其中 old_value **不由 LLM 产出**——教师不会说"旧值是……"，模型也抽不出。
  系统在生成提案时读库填入，写入前再读一次比对，防的是"提案过期/并发覆盖"，
  而不是"教师说错"。

用法：
    python scripts/teacher.py "我们今年拿了 RoboMaster 全国赛一等奖" --lab L01
    python scripts/teacher.py "招新改到大二以上" --lab L01 --apply --yes
    python scripts/teacher.py --rollback snap-20261120-153000
    python scripts/teacher.py --eval          # 跑 14 条指令，算 G-04a/G-04b/G-05
"""
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from llm import chat_json  # noqa: E402

LABS = ROOT / "data/labs/labs.json"
LABS_L1 = ROOT / "data/labs/labs_l1.json"
CHUNKS = ROOT / "data/chunks/chunks.jsonl"
DB = ROOT / "data/indexes/labs_index.sqlite3"
TDIR = ROOT / "data/teacher"
SNAPS = TDIR / "snapshots"
AUDIT = TDIR / "audit.jsonl"

# ---- 闸门①：字段白名单与类型 ----
FIELD_TYPE = {
    "intro": "text", "achievements": "list", "research_areas": "list",
    "group_tags": "list", "recruit_info": "object", "contact": "object",
    "teacher": "scalar", "location": "scalar", "direction_notes": "text",
}
SUBKEYS = {"recruit_info": {"window", "accepts_freshman", "note"},
           "contact": {"qq_group", "email", "wechat"}}
OP_ALLOWED = {"text": {"replace", "append", "remove"},
              "list": {"replace", "append", "remove"},
              "object": {"merge"},
              "scalar": {"replace"}}


def norm(s):
    """规范化比对：去标点空格、统一大小写。"""
    return re.sub(r"[\s，。、；：,.;:!?！？\"'“”‘’()（）\[\]【】]", "", str(s or "")).lower()


# ---- 相对时间归一 ----
# 为什么不让模型算：「今年」在 2027 年再说一遍结果就变了，而年份算错
# 就是一条错成果落进档案。这是确定性换算，模型只负责把话说顺。
REL_YEAR = {"大前年": -3, "大后年": 3, "前年": -2, "后年": 2,
            "去年": -1, "明年": 1, "今年": 0, "本年": 0}


def resolve_time(s):
    """把「今年/去年/前年/明年」换成具体年份：今年 → 2026年。"""
    if not isinstance(s, str) or not s:
        return s
    y = datetime.now().year
    # 长词优先：「大前年」不能被「前年」先吃掉
    for w in ("大前年", "大后年", "前年", "后年", "去年", "明年", "今年", "本年"):
        if w in s:
            s = s.replace(w, f"{y + REL_YEAR[w]}年")
    return s


def resolve_time_deep(v):
    """递归处理 new_value：字符串、数组、对象子键都要换。"""
    if isinstance(v, str):
        return resolve_time(v)
    if isinstance(v, list):
        return [resolve_time_deep(x) for x in v]
    if isinstance(v, dict):
        return {k: resolve_time_deep(x) for k, x in v.items()}
    return v


# ---- 数量汇总陈述 ----
# 「我们总共获得了 100000 个奖」：没有具体奖项名，但它不是编造，是老师在报总数。
# 模型容易判成"内容不足"而追问——和 resolve_time 一样，这类判断不该交给概率模型。
AGG_HEAD = "累计获得各类"
AGG_PREFIX = AGG_HEAD + "奖项"
_NUM = r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+)"
_AGG_COUNT = re.compile(
    rf"({_NUM})\s*(?:多|余)?\s*(个|项|次|件|份|篇|块)"
    rf"[^，。；、\d]{{0,6}}?(奖项|奖|荣誉|证书|成果|专利|论文)")
_AGG_KIND = {"奖": "奖项"}
# 有这些就是在说某一件具体的事，不是汇总
_DETAIL = re.compile(r"(?:[一二三特]等奖|金奖|银奖|铜奖|优胜奖|优秀奖|冠军|亚军|季军"
                     r"|杯|竞赛|大赛|锦标赛|联赛|赛区|国赛|省赛|邀请赛|挑战赛)")


def aggregate_item(text):
    """「总共获得 100000 个奖」→「累计获得各类奖项 100000 项」，不是汇总句则 None。"""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t or _DETAIL.search(t):
        return None
    m = _AGG_COUNT.search(t)
    if not m:
        if AGG_HEAD not in t:        # 老师直接复述已有汇总条目的句式
            return None
        seg = t[t.index(AGG_HEAD) + len(AGG_HEAD):]
        m = re.search(rf"^([^\d]{{0,4}}?)({_NUM})\s*(个|项|次|件|份|篇|块)?", seg)
        if not m:
            return None
        kind, n, unit = m.group(1), m.group(2), m.group(3) or "项"
    else:
        n, unit, kind = m.group(1), m.group(2), m.group(3)
    kind = _AGG_KIND.get(kind, kind) or "奖项"
    return f"{AGG_PREFIX.replace('奖项', kind)} {n} {'项' if unit == '个' else unit}"


def _agg_index(items):
    """库里已有的汇总条目下标（汇总只该有一条）。"""
    for i, x in enumerate(items or []):
        if isinstance(x, str) and x.startswith(AGG_HEAD):
            return i
    return None


# ---- "这个数字像不像真的" ----
# 该追问要追问，但判据得说清楚，否则"30 项省级奖"也会被拦下来问一遍——
# 那是噪音。两个口径：单笔超过 1000（一条记录装不下），或超过现有成果条目 50 倍。
AGG_SUSPECT_ABS = 1000
AGG_SUSPECT_MUL = 50
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn2int(s):
    """「三十」→30，「一万」→10000。取不出来返回 None。"""
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = section = number = 0
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            if u == 10000:
                total += (section + number) * u
                section = number = 0
            else:
                section += (number or 1) * u
                number = 0
        else:
            return None
    return total + section + number


def agg_suspect(item, existing_n):
    """汇总条目里的数量是否明显不像真的。"""
    m = re.search(rf"({_NUM})\s*(?:个|项|次|件|份|篇|块)", str(item or ""))
    if not m:
        return False
    n = cn2int(m.group(1))
    if n is None:
        return False
    return n > AGG_SUSPECT_ABS or n > AGG_SUSPECT_MUL * max(existing_n, 1)


def load_labs():
    return json.loads(LABS.read_text(encoding="utf-8"))


def save_labs(labs):
    txt = json.dumps(labs, ensure_ascii=False, indent=2)
    LABS.write_text(txt, encoding="utf-8")
    if LABS_L1.exists():
        try:
            d = json.loads(LABS_L1.read_text(encoding="utf-8"))
            idx = {x["lab_id"]: x for x in d}
            for l in labs:
                if l["lab_id"] in idx:
                    idx[l["lab_id"]].update({k: l[k] for k in FIELD_TYPE if k in l})
            LABS_L1.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[warn] labs_l1 同步失败：{e}", file=sys.stderr)


# ---------------- 解析 ----------------
SYS = """你在帮实验室老师修改实验室档案。老师会说一句话，你要把它变成结构化 patch。

今天是 {today}（{year} 年）。指令里出现相对时间要换算成具体年份写进 new_value：
今年 → {year}年，去年 → {last}年，前年 → {last2}年，明年 → {next}年。
例：「获得今年挑战杯金奖」→ new_value 写「{year}年挑战杯金奖」，不要原样保留「今年」。

可改字段（只能用这些，其它一律不生成）：
- intro（文本）, achievements（数组）, research_areas（数组）, group_tags（数组）
- recruit_info（对象，子键 window/accepts_freshman/note；accepts_freshman 取值 yes/likely/senior_only/unclear）
- contact（对象，子键 qq_group/email/wechat）
- teacher（文本）, location（文本）

op 规则（必须严格遵守）：
- replace：整体替换，仅用于 文本/数组/单值字段
- append：往数组或文本里追加，new_value 只给新增内容
- remove：从数组或文本里删除，new_value 只给要删的内容
- merge：**仅用于对象字段**（recruit_info/contact），new_value 只含要改的子键

判断原则：
- 一句话里有多个改动 → 输出多个 patch
- **数量汇总也算一条成果**：老师说"总共/累计/一共 获得了 N 个奖"，句中**没有**具体奖项名或赛事名
  → 这是统计陈述，不是编造。append 到 achievements，new_value 写「累计获得各类奖项 N 项」。
  汇总数字和具体奖项是两回事，可以同时存在，不冲突。
- **但数字明显不像真的时，要问一句**。口径：单笔超过 1000 项，或比现有成果条目大出两个数量级
  （例如现有 5 条成果却报 10 万个）。几十、几百项这类常见规模直接用 patch，**不要问**。
  问法很重要：**不要** clarify 去要"具体奖项名称/年份/级别"——老师要说的就是总量，他给不出明细。
  改用 confirm：照原话给出 patch，同时问一句"确认按这个数写吗"。老师看一眼就能决定。
- 老师说"看着改/有点变化/更新一下成果"这类**没给具体内容**的 → 不要猜，返回 clarify
- 老师要删的东西在给定内容里找不到 → 也返回 clarify，并说明"没找到"，不要编造

只输出 JSON：
{"action":"patch","patches":[{"field":"...","op":"...","new_value":...,"reason":"..."}]}
或（内容清楚但某个数字看着可疑：给出 patch，同时问一句确认）
{"action":"confirm","question":"...","patches":[...]}
或
{"action":"clarify","question":"...","reason":"..."}"""


def _sys_prompt():
    """SYS 里有 JSON 示例（含花括号），不能用 .format，只能定点替换。"""
    y = datetime.now().year
    return (SYS.replace("{today}", datetime.now().strftime("%Y-%m-%d"))
               .replace("{year}", str(y)).replace("{last}", str(y - 1))
               .replace("{last2}", str(y - 2)).replace("{next}", str(y + 1)))


def parse(text, lab):
    ctx = {k: lab.get(k) for k in ("lab_id", "name", "intro", "achievements",
                                   "research_areas", "group_tags", "recruit_info",
                                   "contact", "teacher", "location")}
    data = chat_json([
        {"role": "system", "content": _sys_prompt()},
        {"role": "user", "content": json.dumps({"instruction": resolve_time(text), "lab": ctx},
                                               ensure_ascii=False)[:6000]},
    ], max_tokens=800)
    return data


# ---------------- 闸门 ----------------
def gate1(proposals):
    """闸门①：字段白名单 + op 与类型匹配 + 结构完整。"""
    for p in proposals:
        f = p.get("field")
        if f not in FIELD_TYPE:
            return f"field_not_allowed: {f}"
        op = p.get("op")
        if op not in OP_ALLOWED[FIELD_TYPE[f]]:
            return f"proposal_invalid: {f} 不支持 {op}"
        if "new_value" not in p or p["new_value"] in (None, "", [], {}):
            return f"proposal_invalid: {f} 的 new_value 为空"
        if FIELD_TYPE[f] == "object":
            bad = set(p["new_value"]) - SUBKEYS[f]
            if bad or not p["new_value"]:
                return f"proposal_invalid: {f} 子键非法 {bad}"
    return None


def gate3(proposal, lab, current):
    """闸门③：层1 基线 + 层2 操作校验。old_value 由系统注入，这里比对现值。"""
    f, op, v = proposal["field"], proposal["op"], proposal["new_value"]
    old = proposal.get("old_value")

    # 层 1：基线
    if json.dumps(old, ensure_ascii=False, sort_keys=True) != \
       json.dumps(current, ensure_ascii=False, sort_keys=True):
        return "stale_value"

    # 层 2：操作
    if op == "merge":
        if not isinstance(current, dict):
            return "proposal_invalid"
        bad = set(v) - SUBKEYS[f]
        return f"proposal_invalid: {bad}" if bad else None
    if op == "append":
        cur = current if isinstance(current, list) else [current]
        if any(norm(x) == norm(v) for x in cur):
            return "dup_append"
        return None
    if op == "remove":
        cur = current if isinstance(current, list) else [current]
        if not any(norm(x) == norm(v) for x in cur):
            # 找不到时先给"相近项"提示，让教师确认，而不是直接失败
            near = [x for x in cur if norm(v)[:4] and norm(v)[:4] in norm(x)]
            return "value_not_found" + (f"（库里相近的是：{near[0]}）" if near else "")
        return None
    return None


def apply_value(current, op, v):
    if op == "replace":
        return v
    if op == "merge":
        out = dict(current or {})
        out.update(v)
        return out
    if op == "append":
        if isinstance(current, list):
            return list(current) + [v]
        return (str(current or "") + str(v)).strip()
    if op == "remove":
        if isinstance(current, list):
            return [x for x in current if norm(x) != norm(v)]
        return str(current or "").replace(str(v), "").strip()
    return current


# ---------------- 快照 / 审计 ----------------
SNAP_FILES = {"labs.json": LABS, "chunks.jsonl": CHUNKS, "index.sqlite3": DB}


def snapshot(tag=""):
    """写前快照：数据 + 分块 + 索引一起存。

    只存 labs.json 是不够的——回滚后分块与向量仍是新内容，
    学生端检索会拿着旧 id 找不到向量（实际踩到过 KeyError）。
    """
    SNAPS.mkdir(parents=True, exist_ok=True)
    sid = f"snap-{datetime.now().strftime('%Y%m%d-%H%M%S')}" + (f"-{tag}" if tag else "")
    d = SNAPS / sid
    d.mkdir(parents=True, exist_ok=True)
    for name, src in SNAP_FILES.items():
        if src.exists():
            shutil.copy2(src, d / name)
    return sid


def audit(rec):
    TDIR.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def sync_index(lab):
    """把改动同步到检索层，否则学生端还是旧内容。"""
    if not CHUNKS.exists():
        return
    lines = [l for l in CHUNKS.read_text(encoding="utf-8").splitlines() if l.strip()]
    keep, dropped = [], []
    for ln in lines:
        c = json.loads(ln)
        if c.get("lab_id") == lab["lab_id"] and c.get("field") in FIELD_TYPE:
            dropped.append(c["chunk_id"])
        else:
            keep.append(ln)
    # 重新生成该实验室的字段块
    new = []
    for field in FIELD_TYPE:
        val = lab.get(field)
        if not val:
            continue
        if isinstance(val, list):
            for i, item in enumerate(val):
                new.append({"chunk_id": f'{lab["lab_id"]}:{field}:{i:02d}', "lab_id": lab["lab_id"],
                            "lab_name": lab["name"], "field": field, "text": str(item),
                            "page_idx": 0, "bbox": None, "duplicate_of": None,
                            "field_source": "teacher_edit"})
        elif isinstance(val, dict):
            txt = "；".join(f"{k}:{v}" for k, v in val.items() if v)
            new.append({"chunk_id": f'{lab["lab_id"]}:{field}:00', "lab_id": lab["lab_id"],
                        "lab_name": lab["name"], "field": field, "text": txt,
                        "page_idx": 0, "bbox": None, "duplicate_of": None,
                        "field_source": "teacher_edit"})
        else:
            new.append({"chunk_id": f'{lab["lab_id"]}:{field}:00', "lab_id": lab["lab_id"],
                        "lab_name": lab["name"], "field": field, "text": str(val),
                        "page_idx": 0, "bbox": None, "duplicate_of": None,
                        "field_source": "teacher_edit"})
    allc = [json.loads(x) for x in keep] + new
    CHUNKS.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in allc) + "\n", encoding="utf-8")

    if not DB.exists():
        return
    from build_chunks import space_cjk
    con = sqlite3.connect(DB)
    cur = con.cursor()
    for cid in dropped:
        cur.execute("DELETE FROM chunk_meta WHERE chunk_id=?", (cid,))
        cur.execute("DELETE FROM chunk_fts WHERE chunk_id=?", (cid,))
        cur.execute("DELETE FROM chunk_vec WHERE chunk_id=?", (cid,))   # 否则留下孤儿向量
    for c in new:
        cur.execute("INSERT OR REPLACE INTO chunk_meta VALUES (?,?,?,?,?,?,?,?,?)",
                    (c["chunk_id"], c["lab_id"], c["lab_name"], c["field"], c["text"],
                     c["page_idx"], json.dumps(c["bbox"]), c["duplicate_of"], c["field_source"]))
        cur.execute("DELETE FROM chunk_fts WHERE chunk_id=?", (c["chunk_id"],))
        cur.execute("INSERT INTO chunk_fts(text, chunk_id) VALUES (?,?)",
                    (space_cjk(c["text"]), c["chunk_id"]))
    con.commit()
    con.close()

    # 新块没有向量会导致检索 KeyError（踩过），增量补齐
    try:
        import build_vectors
        build_vectors.build()
    except Exception as e:
        print(f"[warn] 向量补齐失败（检索会降级）：{type(e).__name__}: {e}", file=sys.stderr)


# ---------------- 主流程 ----------------
def _to_proposals(data, lab, lab_id, user):
    """把模型输出的 patches 展开成提案，old_value 一律由系统注入。"""
    proposals = []
    for p in data.get("patches") or []:
        f, op = p.get("field"), p.get("op")
        # 模型可能把"今年"原样搬进 new_value，这里再兜一次底
        v = resolve_time_deep(p.get("new_value"))
        current = lab.get(f)

        # 规范化：append/remove 的 new_value 若是数组，展开成多条提案
        # （模型常把"新增一项"写成 ["xxx"]，直接拼会得到嵌套数组）
        vals = v if isinstance(v, list) and op in ("append", "remove") else [v]
        for val in vals:
            proposals.append({                   # 系统注入 old_value
                "lab_id": lab_id, "field": f, "op": op,
                "old_value": current, "new_value": val,
                "reason": p.get("reason"), "user": user,
            })
    return proposals


def _aggregate_fallback(text, lab, lab_id, user):
    """模型仍然追问时的兜底：确认是数量汇总句，就直接给一条 append 提案。

    为什么要有这层：提示词说了它也可能不听（尤其数字离谱时，模型倾向"这不真实"）。
    但老师说的是档案内容还是假的，不由模型裁决——他自己会看 diff、点确认。
    """
    item = aggregate_item(text)
    if not item:
        return []
    return [{"lab_id": lab_id, "field": "achievements", "op": "append",
             "old_value": lab.get("achievements"), "new_value": item,
             "reason": "数量汇总陈述", "user": user}]


def _dedupe_aggregate(proposals, lab):
    """汇总条目只保留一条：库里已有的话，append 改成「删旧的 + 加新的」。"""
    out = []
    for p in proposals:
        if (p["field"] == "achievements" and p["op"] == "append"
                and isinstance(p["new_value"], str)
                and p["new_value"].startswith(AGG_HEAD)):
            i = _agg_index(lab.get("achievements"))
            if i is not None:
                old = lab["achievements"][i]
                if norm(old) == norm(p["new_value"]):
                    continue                       # 数字没变，没必要改
                out.append({**p, "op": "remove", "new_value": old, "reason": "汇总数字更新"})
        out.append(p)
    return out


def propose(text, lab_id, user="teacher"):
    """生成提案。三种结果：直接提案 / 需确认（confirm）/ 需补充（clarify）。

    confirm 是这一版新增的中间态：数字看着可疑时，追问和要求补充明细是两回事。
    追问有价值（防止手误把 1000 打成 100000），但追问必须**带着出口**——
    老师看一眼"就按这个写"就能过去，而不是被迫重新组织一遍语言。
    """
    labs = load_labs()
    lab = next((l for l in labs if l["lab_id"] == lab_id), None)
    if not lab:
        return {"ok": False, "code": "lab_mismatch", "msg": f"库中没有 {lab_id}"}

    data = parse(text, lab)
    proposals = _to_proposals(data, lab, lab_id, user)

    if not proposals:
        # 模型坚持要明细（clarify）→ 如果这是数量汇总句，我们自己给出提案，让流程别死在这里。
        proposals = _aggregate_fallback(text, lab, lab_id, user)
        if not proposals:
            return {"ok": False, "code": "clarify",
                    "question": data.get("question") or "能具体说要改哪个字段、改成什么吗？"}
        data["action"] = "confirm"        # 兜底一律先给出口，由下面的口径决定要不要真问

    proposals = _dedupe_aggregate(proposals, lab)
    if not proposals:
        return {"ok": False, "code": "nochange",
                "question": "档案里已经是这个数字了，不用改。"}

    err = gate1(proposals) or gate_gate2(proposals, lab_id)
    if err:
        return {"ok": False, "code": err, "proposals": proposals}

    # 是否值得问一句：只由数量级决定，不看模型心情。
    # 模型想说 confirm 但数字其实正常 → 降回普通提案（老师报 30 项不想被盘问）。
    old = lab.get("achievements") or []
    existing_n = len(old) - (1 if _agg_index(old) is not None else 0)
    suspect = next((p for p in proposals
                    if p["field"] == "achievements" and p["op"] == "append"
                    and isinstance(p["new_value"], str)
                    and p["new_value"].startswith(AGG_HEAD)
                    and agg_suspect(p["new_value"], existing_n)), None)

    if suspect:
        q = data.get("question") if data.get("action") == "confirm" else ""
        return {"ok": False, "code": "confirm",
                "question": q or f"这个数量（{suspect['new_value']}）比档案里现有的 {existing_n} 条成果大出很多，"
                                 f"确认按这个数写吗？",
                "proposals": proposals, "lab": lab}
    return {"ok": True, "lab": lab, "proposals": proposals}


def gate_gate2(proposals, bound_lab):
    for p in proposals:
        if p.get("lab_id") != bound_lab:
            return f"lab_mismatch: {p.get('lab_id')} ≠ {bound_lab}"
    return None


def show_diff(lab, proposals):
    lines = []
    for p in proposals:
        f, op, v = p["field"], p["op"], p["new_value"]
        cur = lab.get(f)
        if op == "merge":
            for k, nv in v.items():
                lines.append(f'  {f}.{k}: {cur.get(k)!r} → {nv!r}')
        elif op == "append":
            lines.append(f"  {f}: + {v!r}")
        elif op == "remove":
            lines.append(f"  {f}: - {v!r}")
        else:
            lines.append(f"  {f}: {str(cur)[:40]!r} → {str(v)[:40]!r}")
    return "\n".join(lines)


def apply_proposals(proposals, lab_id, user="teacher", instruction=""):
    """按**已确认的提案**写入（不再重跑模型）。

    教师页展示的 diff 必须与实际落库的内容完全一致——如果 apply 时重新解析一遍，
    模型可能给出不同的 patch，用户确认的和他看到的就是两回事。
    """
    err = gate1(proposals) or gate_gate2(proposals, lab_id)
    if err:
        return {"ok": False, "code": err, "proposals": proposals}

    labs = load_labs()
    cur_lab = next((l for l in labs if l["lab_id"] == lab_id), None)
    if not cur_lab:
        return {"ok": False, "code": "lab_mismatch"}

    for p in proposals:
        p["lab_id"] = lab_id
        err = gate3(p, cur_lab, cur_lab.get(p["field"]))
        if err:
            return {"ok": False, "code": err, "proposals": proposals}

    diff_before = show_diff(cur_lab, proposals)
    sid = snapshot(lab_id)
    for p in proposals:
        cur = cur_lab.get(p["field"])
        cur_lab[p["field"]] = apply_value(cur, p["op"], p["new_value"])
    cur_lab["updated_at"] = datetime.now().strftime("%Y-%m-%d")
    save_labs(labs)
    sync_index(cur_lab)
    audit({"ts": datetime.now().isoformat(timespec="seconds"), "user": user,
           "lab_id": lab_id, "instruction": instruction, "snapshot": sid,
           "patches": [{k: p[k] for k in ("field", "op", "old_value", "new_value")} for p in proposals]})
    return {"ok": True, "snapshot": sid, "lab": cur_lab, "proposals": proposals,
            "diff": diff_before}


def apply(text, lab_id, user="teacher", confirmed=False):
    r = propose(text, lab_id, user)
    if not r["ok"]:
        return r
    if not confirmed:
        return {"ok": False, "code": "not_confirmed", "proposals": r["proposals"],
                "diff": show_diff(r["lab"], r["proposals"])}
    return apply_proposals(r["proposals"], lab_id, user, instruction=text)


def rollback(sid):
    d = SNAPS / sid
    if not d.exists():
        return {"ok": False, "code": "snapshot_not_found"}
    for name, dst in SNAP_FILES.items():
        src = d / name
        if src.exists():
            shutil.copy2(src, dst)
    same = all((d / n).read_bytes() == f.read_bytes()
               for n, f in SNAP_FILES.items() if (d / n).exists())

    # 用掉的快照从列表移除：它的内容此刻就等于库的现状，留着只会让老师
    # 面对一串"回滚了也没变化"的条目，分不清哪个还能用。
    # 只在逐字一致时删——回滚失败要保留现场供排查。
    if same:
        shutil.rmtree(d, ignore_errors=True)
    # 快照名带实验室后缀，审计里补上，否则前端只能显示"全局"
    lab_id = sid.rsplit("-L", 1)[-1]
    audit({"ts": datetime.now().isoformat(timespec="seconds"), "user": "system",
           "action": "rollback", "snapshot": sid,
           "lab_id": ("L" + lab_id) if lab_id != sid else None,
           "identical": same})
    return {"ok": same, "identical": same}


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(0)
    if "--rollback" in a:
        sid = a[a.index("--rollback") + 1]
        print(json.dumps(rollback(sid), ensure_ascii=False, indent=1))
        sys.exit(0)
    if "--eval" in a:
        from teacher_eval import main as eval_main
        eval_main()
        sys.exit(0)

    text = a[0]
    lab = a[a.index("--lab") + 1] if "--lab" in a else None
    if not lab:
        print("必须指定 --lab（教师只能改自己实验室）")
        sys.exit(2)
    if "--apply" in a:
        r = apply(text, lab, confirmed="--yes" in a)
    else:
        r = propose(text, lab)
        if r.get("ok"):
            r["diff"] = show_diff(r["lab"], r["proposals"])
    print(json.dumps({k: v for k, v in r.items() if k != "lab"}, ensure_ascii=False, indent=1))
