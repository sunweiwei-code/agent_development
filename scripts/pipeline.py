# -*- coding: utf-8 -*-
"""
M1 学生链路：抽取 → 追问 → 检索 → 打分 → 阈值/门禁 → 四种输出形态。

对应 PRD：
  5.6 槽位（interest / goal / tech_stack / grade / preferred_teacher / target_contest）
  8.1 打分（方向 0.35 / 路径 0.25 / 技术栈 0.20 / 年级 0.10 / 教师比赛 0.10，缺失记 0 不归一化）
  8.1 方向门禁：direction > 0 才可进 Top3/others；形态④ 豁免
  8.4 输出形态 ①②③④
  十一 异常清单：画像空白 → 追问 1 次 → 仍空白走形态④

设计取舍：
  · 抽取用 LLM（口语理解），打分用规则（可解释、可复现、可回归）——两者职责分开
  · 检索只负责"圈定候选"（RRF 取前 20），Top8/Top3 完全由规则分决定
  · 本模块不依赖前端，先跑通后端再用评测集验证
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import chat_json
from reasons import generate as generate_reasons, generate_streamed as generate_reasons_streamed
from score import score_lab

ROOT = Path(__file__).resolve().parents[1]
MIN_SCORE = 0.35
TOP_N_RETRIEVE = 20
TOP_K = 3

SLOT_DEF = """请从学生的话里抽取以下 6 个槽位。**只抽取原文明确表达的信息，不要推断、不要补全**。
- interest: 想做的技术方向（如「视觉算法」「数据分析」「鸿蒙开发」）。没说就 null
- goal: 主要诉求，只能是 "科研" / "竞赛" / "就业" / "未定" 之一。没说就 null
- tech_stack: 提到的具体技术、语言、工具（如 "Python" "C++" "PyTorch"）。没说就 null
- grade: 年级，只能是 "大一"/"大二"/"大三"/"大四"/"研究生" 之一。没说就 null
- preferred_teacher: 提到的老师姓名。没说就 null
- target_contest: 提到的目标比赛（如 "RoboMaster" "ACM"）。没说就 null

示例：
输入「我喜欢打算法竞赛，想进能带 ACM 的实验室」
→ {"interest":"算法竞赛","goal":"竞赛","tech_stack":null,"grade":null,"preferred_teacher":null,"target_contest":"ACM"}

输入「对 AI 感兴趣但没基础」
→ {"interest":"人工智能","goal":null,"tech_stack":null,"grade":null,"preferred_teacher":null,"target_contest":null}

输入「随便看看」
→ {"interest":null,"goal":null,"tech_stack":null,"grade":null,"preferred_teacher":null,"target_contest":null}

只输出 JSON，不要解释：
{"interest":..., "goal":..., "tech_stack":..., "grade":..., "preferred_teacher":..., "target_contest":...}"""


# 赛事名 → 方向词。学生说"我想参加ACM比赛"时，赛事本身就是方向信号，
# 不能因为没说出"算法竞赛"四个字就去追问（实测这里会卡住）。
CONTEST_TO_INTEREST = {
    "acm": "算法竞赛", "icpc": "算法竞赛", "蓝桥杯": "程序设计",
    "robomaster": "机器人", "robocon": "机器人",
    "数学建模": "数学建模", "电赛": "电子设计", "电子设计": "电子设计",
    "智能车": "智能车", "挑战杯": "创新创业", "互联网+": "创新创业",
}


def _contest_to_interest(contest):
    if not contest:
        return None
    c = str(contest).lower().replace(" ", "")
    for k, v in CONTEST_TO_INTEREST.items():
        if k in c:
            return v
    return None


def extract_slots(text):
    data = chat_json([
        {"role": "system", "content": SLOT_DEF},
        {"role": "user", "content": text},
    ], max_tokens=300)
    # 归一化：空字符串 → None；未知值收敛
    out = {}
    for k in ("interest", "goal", "tech_stack", "grade", "preferred_teacher", "target_contest"):
        v = data.get(k)
        if isinstance(v, str):
            v = v.strip()
            v = None if v.lower() in ("", "null", "none", "未知", "未定 " ) else v
        out[k] = v
    if out["goal"] not in ("科研", "竞赛", "就业", "未定"):
        out["goal"] = "未定" if out["goal"] is None else out["goal"]

    # 关键词兜底：LLM 对"我未来打算考研"这种短句常给"未定"，
    # 但"考研/保研/读研"的诉求非常明确，不能依赖模型猜。
    if out["goal"] in (None, "未定"):
        if any(h in text for h in ("考研", "保研", "读研", "研究生", "升学", "直博", "读博", "做科研", "搞科研")):
            out["goal"] = "科研"
        elif any(h in text for h in ("就业", "找工作", "实习", "进大厂", "直接工作", "入职")):
            out["goal"] = "就业"
        elif any(h in text.lower() for h in ("比赛", "竞赛", "参赛", "省赛", "国赛", "acm", "robomaster")):
            out["goal"] = "竞赛"

    # 只说赛事、没说方向时，把赛事折算成方向，否则会被判"信息不足"去追问
    if not out.get("interest") and out.get("target_contest"):
        out["interest"] = _contest_to_interest(out["target_contest"])
    return out


def missing_slots(slots):
    """哪些关键槽位缺失。

    只把 interest 缺失视为"必须追问"：方向是核心维度（方向门禁也要求 direction>0）。
    goal 缺失或"未定"不追问——8.1 的路径映射对"未定"给 0.5 中性分，仍可出推荐。
    （Q-08"对 AI 感兴趣但没基础"就是这种：有方向无 goal，应给推荐而非追问）
    """
    miss = []
    if not slots.get("interest"):
        miss.append("interest")
    return miss


def followup_question(slots):
    """只问一个最关键的问题，不连环问。"""
    if not slots.get("interest"):
        return "你想做偏哪个方向？比如视觉算法、数据分析、嵌入式、网络安全，或者你感兴趣的具体技术都行。"
    if not slots.get("goal"):
        return "你更看重哪一点——想进实验室做科研、想打比赛，还是想积累项目经验方便就业？"
    return None


# ============ 意图：推荐 vs 具体咨询某个实验室 ============
# 学生说"具体介绍一下腾讯安全英才班实验室"时，不该再推一批候选——那是在答非所问。
DETAIL_HINTS = ("介绍", "讲一下", "讲讲", "详细", "怎么样", "做什么", "干什么",
                "情况", "了解", "看看", "说说", "在做什么", "能做什么")
PRONOUN_HINTS = ("这个实验室", "该实验室", "这个组", "这家", "它", "刚才那个", "第一个", "第二个", "第三个")


def _norm_name(s):
    import re as _re
    s = _re.sub(r"[（(][^）)]*[）)]", "", s or "")
    return _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s)


def match_lab(text, labs, history=None):
    """从一句话里认出"说的是哪个实验室"。认不出返回 None。

    策略：① 名称包含匹配（去括号与标点后比对，取最长）② 指代词 → 用上一轮推荐的第一条
    """
    t = _norm_name(text)
    best, best_len = None, 0
    for l in labs:
        n = _norm_name(l["name"])
        if not n:
            continue
        if n in t or (len(n) >= 6 and n[:6] in t):
            if len(n) > best_len:
                best, best_len = l, len(n)
        # 简称："腾讯安全英才班" → 命中 L21
        short = n.replace("实验室", "").replace("创新", "")
        if len(short) >= 5 and short in t and len(short) > best_len:
            best, best_len = l, len(short)
    if best:
        return best
    if history and any(h in text for h in PRONOUN_HINTS):
        ids = (history.get("labs") or [])
        # ① 序数指代优先
        for i, w in enumerate(("第一个", "第二个", "第三个")):
            if w in text and len(ids) > i:
                return next((l for l in labs if l["lab_id"] == ids[i]), None)
        # ② 唯一候选
        if len(ids) == 1:
            return next((l for l in labs if l["lab_id"] == ids[0]), None)
        # ③ 最近看过/排在首位的那个
        last = history.get("last_lab") or (ids[0] if ids else None)
        if last:
            return next((l for l in labs if l["lab_id"] == last), None)
    return None


ORD_HINTS = ("第一个", "第二个", "第三个")

# 去掉这些填充词后若几乎不剩内容，说明是"详细介绍一下"这类**无所指**的追问
FILLER_WORDS = ("详细", "具体", "介绍一下", "介绍", "一下", "说说", "讲讲", "讲讲看",
                "再说说", "再多说", "这个", "那个", "它", "的", "呢", "吧", "啊",
                "怎么样", "情况", "能", "不能", "看看", "我想", "知道", "点")


def _pronoun_like(text):
    t = text
    for w in FILLER_WORDS:
        t = t.replace(w, "")
    return len(t.strip("，。,！？ 、")) <= 1

# ============ 意图：实验室对比 ============
COMPARE_HINTS = ("对比", "比较", "比一下", "比一比", "区别", "差异", "哪个更", "哪个好",
                 "哪个适合", "相比", "vs", "VS", "Vs", "pk", "PK")


def compare_labs(text, labs, history=None):
    """认出"要对比哪几个实验室"。不足 2 个返回 None。"""
    # 两个序数并提（"第一个和第二个"）本身就是对比信号，不必出现"对比"二字
    ord_cnt = sum(1 for o in ORD_HINTS if o in text)
    if not any(h in text for h in COMPARE_HINTS) and ord_cnt < 2:
        return None
    t = _norm_name(text)
    found, seen = [], set()

    # ① 名称匹配：全名 / 前 6 字 / **唯一前缀**
    # 唯一前缀是必须的——学生常说简称，会省掉中间的字：
    #   "三维感知实验室" vs 全名"三维感知技术实验室"，前 6 字对不上，但对前 4 字
    # 只在前 4 字**没有歧义**时才认，避免"人工智能…"这种一串实验室共享的前缀被误配
    prefix_owner = {}
    for l in labs:
        n = _norm_name(l["name"])
        if len(n) >= 4:
            prefix_owner.setdefault(n[:4], []).append(l)

    for l in labs:
        n = _norm_name(l["name"])
        if not n:
            continue
        hit = n in t or (len(n) >= 6 and n[:6] in t)
        if not hit and len(n) >= 4 and n[:4] in t:
            hit = len(prefix_owner.get(n[:4], [])) == 1      # 前 4 字唯一才认
        if hit and l["lab_id"] not in seen:
            seen.add(l["lab_id"]); found.append(l)

    # ② 序数指代（"第一个和第二个比一下"）
    if len(found) < 2 and history:
        ids = history.get("labs") or []
        for i, w in enumerate(ORD_HINTS):
            if w in text and len(ids) > i:
                l = next((x for x in labs if x["lab_id"] == ids[i]), None)
                if l and l["lab_id"] not in seen:
                    seen.add(l["lab_id"]); found.append(l)
        # ③ 数量/集合指代："头两个""前两个""这两个""这三个""它们""都"
        if len(found) < 2:
            want = None
            m = re.search(r"([两二三四2-4])\s*个", text)
            if m:
                want = {"两": 2, "二": 2, "三": 3, "四": 4}.get(m.group(1), int(m.group(1)) if m.group(1).isdigit() else None)
            elif any(w in text for w in ("这几个", "那些", "它们", "这些都", "全都")):
                want = min(3, len(ids))
            if want:
                # "后两个/后三个" 取末尾，"前/头/这/那 N 个" 取开头
                pick = ids[-want:] if ("后" in text and "后" in text[:text.find("个")+1]) else ids[:want]
                for lid in pick:
                    l = next((x for x in labs if x["lab_id"] == lid), None)
                    if l and l["lab_id"] not in seen:
                        seen.add(l["lab_id"]); found.append(l)

    return found[:3] if len(found) >= 2 else None


def build_compare(labs_):
    """把 2–3 个实验室整理成对比表所需的行。"""
    def vals(fn):
        return [fn(l) for l in labs_]

    def achievements(l):
        ach = l.get("achievements") or []
        return ach[:4] if ach else []

    rows = [
        {"key": "areas", "label": "研究方向",
         "values": [list(l.get("research_areas") or [])[:10] for l in labs_]},
        {"key": "path", "label": "主导方向",
         "values": [(l.get("path_type") or ["待确认"])[0] for l in labs_]},
        {"key": "teacher", "label": "指导老师", "values": vals(lambda l: l.get("teacher") or "—")},
        {"key": "location", "label": "地点", "values": vals(lambda l: l.get("location") or "—")},
        {"key": "recruit", "label": "招新",
         "values": [((l.get("recruit_info") or {}).get("accepts_freshman") or "unclear") for l in labs_]},
        {"key": "ach", "label": "代表成果", "values": [achievements(l) for l in labs_]},
        {"key": "contact", "label": "联系方式",
         "values": [((l.get("contact") or {}).get("qq_group") or
                     (l.get("contact") or {}).get("email") or "—") for l in labs_]},
    ]
    return {"labs": [{"lab_id": l["lab_id"], "name": l["name"],
                      "path_type": (l.get("path_type") or ["待确认"])[0]} for l in labs_],
            "rows": rows}


def is_detail_request(text, labs, history=None):
    """判断是否在问"某个实验室的具体情况"。

    两类触发：
      · 明确的咨询词（介绍/怎么样/做什么…）
      · 序数指代（"第一个呢"）——只有在有上一轮候选时才算
    """
    has_hint = any(h in text for h in DETAIL_HINTS)
    ordinal = any(o in text for o in ORD_HINTS) and bool((history or {}).get("labs"))
    if not (has_hint or ordinal):
        return None
    lab = match_lab(text, labs, history)
    if lab:
        return lab
    # 没点名、也没指代词，但整句只剩填充词（"详细介绍一下"）→ 默认指上一轮那个
    if _pronoun_like(text) and history:
        last = history.get("last_lab") or ((history.get("labs") or [None])[0])
        if last:
            return next((l for l in labs if l["lab_id"] == last), None)
    return None


def _history(slots, asked, items):
    """历史里带上本轮推荐的 lab_id，供"它/这个实验室"这类指代使用。"""
    return {"slots": slots, "asked": asked,
            "labs": [t["lab_id"] for t in items],
            "last_lab": items[0]["lab_id"] if items else None}


def _load_labs():
    return [l for l in json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8"))
            if not l.get("duplicate_of")]


def retrieve(query_text, top_k=TOP_N_RETRIEVE):
    """混合检索：向量 + BM25 → RRF。只负责圈候选，不决定排序。"""
    try:
        import sqlite3
        from build_chunks import space_cjk
        from build_vectors import VectorStore, bm25_search, rrf, embed, load_key
        con = sqlite3.connect(ROOT / "data/indexes/labs_index.sqlite3")
        vs = VectorStore()
        qv = embed([query_text], load_key(), text_type="query")[0]
        vec = vs.search(qv, top_k * 2)
        bm = bm25_search(con, query_text, top_k * 2)
        fused = rrf([bm, vec])[:top_k]
        ids = []
        for cid, _ in fused:
            lab_id = vs.meta[cid].split("|")[0]
            if lab_id not in ids:
                ids.append(lab_id)
        con.close()
        return ids
    except Exception as e:
        # 检索不可用时退化为全量打分（44 条规模完全可行），不阻塞主链路
        print(f"[warn] 检索降级为全量：{type(e).__name__}: {e}", file=sys.stderr)
        return [l["lab_id"] for l in _load_labs()]


def rank(slots, lab_ids, labs_by_id):
    rows = []
    for lid in lab_ids:
        lab = labs_by_id.get(lid)
        if not lab:
            continue
        s = score_lab(slots, lab)
        rows.append((lab, s))
    rows.sort(key=lambda x: -x[1]["score"])
    return rows


def _why(lab, s):
    """结构化匹配要点，交给 LLM 说人话时用；不含分数。"""
    pts = []
    if s["direction"] >= 0.8:
        pts.append("方向直接对口")
    elif s["direction"] > 0:
        pts.append("方向相邻")
    if s["path"] >= 0.8:
        pts.append(f"培养路径匹配（{(lab.get('path_type') or ['待确认'])[0]}）")
    if s["grade"] > 0:
        pts.append("对低年级开放")
    if s["tech"] > 0:
        pts.append("技术栈对口")
    if s["pref"] > 0:
        pts.append("与本科学科/目标赛事相关")
    return pts


def _reason(lab, s):
    """规则口径的一句话理由（不依赖 LLM，保证可复现）。"""
    parts = []
    if s["direction"] >= 0.8:
        parts.append(f"方向匹配（{lab['research_areas'][0] if lab['research_areas'] else '—'}）")
    elif s["direction"] > 0:
        parts.append("方向相邻")
    pt = (lab.get("path_type") or ["待确认"])[0]
    if pt in ("科研主导", "竞赛主导", "就业项目主导", "双主导"):
        parts.append(f"{pt}")
    if s["grade"] > 0:
        parts.append("招新对低年级开放")
    if s["tech"] > 0:
        parts.append("技术栈对口")
    return "，".join(parts) if parts else "综合匹配"


def recommend(text, labs=None, history=None, on_step=None, on_token=None):
    """单轮或多轮推荐。

    history: 上一轮返回的 {"slots": {...}, "asked": n}，用于追问后续接。
    追问策略（PRD 十一）：追问 1 次；仍拿不到方向 → 走形态④ 规则兜底。
    注意：形态④ 豁免方向门禁——它本来就不看方向，否则空白画像永远给不出推荐。
    """
    labs = labs or _load_labs()
    by_id = {l["lab_id"]: l for l in labs}
    history = history or {"slots": {}, "asked": 0}
    emit = on_step or (lambda *a, **k: None)

    # 先判"对比"——对比请求里往往也带"怎么样/哪个好"，必须优先于详情
    cmp_labs = compare_labs(text, labs, history)
    if not cmp_labs and any(h in text for h in COMPARE_HINTS):
        # 说了"对比"但认不出是哪几个 → 直接问清楚，别掉回推荐流程答非所问
        return {"form": "追问", "slots": history.get("slots") or {}, "missing": ["lab"],
                "followup_question": "要对比哪几个实验室？可以说名字，或者先把方向告诉我，我先推荐几个再挑。",
                "top3": [], "others": [], "no_match_reason": None,
                "fallback_basis": None, "history": history}
    if cmp_labs:
        emit("compare", f"识别为对比：{' vs '.join(l['name'] for l in cmp_labs)}")
        emit("form", "形态：对比")
        h = dict(history); h["last_lab"] = cmp_labs[0]["lab_id"]
        return {"form": "对比", **build_compare(cmp_labs),
                "slots": history.get("slots") or {}, "missing": [],
                "followup_question": None, "top3": [], "others": [],
                "no_match_reason": None, "fallback_basis": None, "history": h}

    # 再判"具体咨询某个实验室"——这类请求不该再推候选
    lab = is_detail_request(text, labs, history)
    if lab:
        emit("detail", f"识别为咨询：{lab['name']}")
        h = dict(history)
        emit("form", "形态：详情")
        h["last_lab"] = lab["lab_id"]
        return {"form": "详情", "lab": dict(lab), "slots": history.get("slots") or {},
                "missing": [], "followup_question": None, "top3": [], "others": [],
                "no_match_reason": None, "fallback_basis": None,
                "history": h}

    emit("extract", "正在理解你的描述…")
    fresh = extract_slots(text)
    emit("extract_done", "已识别诉求", {k: v for k, v in fresh.items() if v})
    # 与历史合并：新抽到的非空值覆盖旧值，旧值保留（多轮补槽）
    slots = dict(history.get("slots") or {})
    for k, v in fresh.items():
        if not v:
            continue
        # "未定"是"没说清"，不该推翻上一轮已经说清的目标
        if k == "goal" and v == "未定" and (slots.get("goal") or "未定") != "未定":
            continue
        slots[k] = v

    miss = missing_slots(slots)
    asked = int(history.get("asked", 0))
    if miss:
        emit("gate", "方向信息不足", {"missing": miss, "asked": asked})
        if asked >= 1:                       # 追问过一次仍拿不到方向 → 兜底
            fb = fallback_empty_profile(labs)
            fb["slots"] = slots
            fb["missing"] = miss
            fb["followup_question"] = None
            fb["history"] = _history(slots, asked, fb.get("top3") or [])
            fb["notice"] = "没拿到你的方向，先按招新开放度给你几个大一能进的实验室"
            return fb
        # 追问时附 3 个"参考"实验室：不改变形态判定（仍是追问），
        # 但避免"只问一句什么都不给"的空手感。标注为参考，不冒充匹配结果。
        ref = fallback_empty_profile(labs)
        return {"form": "追问", "slots": slots, "missing": miss,
                "followup_question": followup_question(slots),
                "top3": [], "others": [], "no_match_reason": None, "fallback_basis": None,
                "references": ref["top3"],
                "references_note": "先给你三个大一能进的实验室参考，等你说清方向我再精确匹配",
                "history": _history(slots, asked + 1, ref["top3"])}

    emit("retrieve", "正在检索候选实验室…")
    # 检索词必须由**槽位**驱动，而不是原始话语：
    # 多轮里"还有什么类似的吗""那这个方向呢"这类追问不含主题词，用原文检索会跑偏，
    # 结果 L45 根本没进候选，最后误判成"库里没有计算成像"——前后矛盾就是这么来的。
    rq = " ".join(x for x in (slots.get("interest"), slots.get("tech_stack"),
                              slots.get("target_contest"), text) if x)
    cand_ids = retrieve(rq)
    emit("retrieve_done", f"圈定 {len(cand_ids)} 条候选", {"n": len(cand_ids)})

    emit("score", "按方向/路径/技术栈/年级打分…")
    rows = rank(slots, cand_ids, by_id)
    top = [(l["lab_id"], round(s["score"], 2)) for l, s in rows[:5]]
    emit("score_done", "打分完成", {"top5": top})

    # 路径 A：interest 非空且全部候选方向分为 0
    # 但先做一次全量兜底：候选只有 20 条，可能只是没召回，不能据此断言"库里没有"
    if max((s["direction"] for _, s in rows), default=0.0) == 0:
        all_rows = rank(slots, [l["lab_id"] for l in labs], by_id)
        if max((s["direction"] for _, s in all_rows), default=0.0) > 0:
            rows = all_rows
            emit("retrieve_fallback", "候选内无方向命中，已全量重查", {"n": len(rows)})
    if max((s["direction"] for _, s in rows), default=0.0) == 0:
        return {"form": "②", "slots": slots, "missing": [], "followup_question": None,
                "top3": [], "others": [],
                "no_match_reason": f"库里没有与「{slots['interest']}」相关的研究方向",
                "fallback_basis": None,
                "references": fallback_empty_profile(labs)["top3"],
                "references_note": "不过有几个大一就能进、方向较宽的实验室，可以先看看",
                "history": _history(slots, asked, fallback_empty_profile(labs)["top3"])}

    # 方向门禁 + 阈值
    # 方向门禁；但"点名赛事"的诉求（如 RoboMaster）可能方向词完全不出现，
    # 此时允许用赛事命中（pref=1.0）替代方向命中，否则会被误判为"没有相关实验室"
    elig = [(l, s) for l, s in rows
            if (s["direction"] > 0 or s.get("pref", 0) >= 1.0) and s["score"] >= MIN_SCORE]
    if not elig:
        # 形态③：有候选但全低于阈值；others 受门禁约束，direction=0 时给空
        near = [(l, s) for l, s in rows if s["direction"] > 0][:2]
        return {"form": "③", "slots": slots, "missing": [], "followup_question": None,
                "top3": [],
                "others": [{"lab_id": l["lab_id"], "name": l["name"], "score": s["score"]}
                           for l, s in near],
                "no_match_reason": "暂无强匹配，以下是最接近的方向",
                "fallback_basis": None, "history": _history(slots, asked, [])}

    top = elig[:TOP_K]
    items = [{"lab_id": l["lab_id"], "name": l["name"], "score": round(s["score"], 3),
              "path_type": (l.get("path_type") or ["待确认"])[0],
              "research_areas": l.get("research_areas") or [],
              "why_matched": _why(l, s),
              "reason": _reason(l, s),
              "breakdown": {k: s[k] for k in ("direction", "path", "tech", "grade", "pref")}}
             for l, s in top]

    # 流式模式才提前推候选（阻塞模式等 result 一次性给，避免出现"半截卡片"）
    if on_token:
        emit("candidates", "候选已确定", {"items": [
            {"lab_id": t["lab_id"], "name": t["name"], "path_type": t["path_type"],
             "research_areas": (t.get("research_areas") or [])[:8], "score": t["score"]}
            for t in items]})
    emit("reasons", "正在生成推荐理由…")
    # 理由生成（唯一可能引入名称幻觉的环节，生成后立刻过白名单）
    if on_token:
        # 流式：逐实验室边生成边推 token（首字可见，不再干等 2–3 秒）
        texts = generate_reasons_streamed(items, slots, on_token)
    else:
        texts = generate_reasons(items, slots)
    for it in items:
        r = texts.get(it["lab_id"], {})
        it["reason_text"] = r.get("text", it["reason"])
        it["reason_fallback"] = r.get("fallback", True)
        it["dir_warnings"] = r.get("dir_warnings", [])

    emit("form", "形态① · 正常推荐")
    return {"form": "①", "slots": slots, "missing": [], "followup_question": None,
            "top3": items, "others": [], "no_match_reason": None, "fallback_basis": None,
            "history": _history(slots, asked, items)}


def fallback_empty_profile(labs=None):
    """形态④：画像完全空白时的规则兜底（豁免方向门禁）。"""
    labs = labs or _load_labs()
    cand = [l for l in labs
            if (l.get("recruit_info") or {}).get("accepts_freshman") in ("yes", "likely")
            and l.get("confidence") == "high"
            and l.get("contact")]
    cand.sort(key=lambda l: 0 if (l["recruit_info"]["accepts_freshman"] == "yes") else 1)
    return {"form": "④",
            "top3": [{"lab_id": l["lab_id"], "name": l["name"],
                      "path_type": (l.get("path_type") or ["待确认"])[0],
                      "research_areas": l.get("research_areas") or [],
                      "reason": "招新对大一开放，信息完整，适合先从这些了解",
                      "reason_text": "", "score": None, "breakdown": {}}
                     for l in cand[:3]],
            "fallback_basis": "按招新开放度与信息完整度兜底，非方向匹配结果",
            "no_match_reason": None, "others": [], "slots": {}, "missing": [],
            "followup_question": None, "history": _history({}, 0, [])}


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "我想做视觉算法，想打比赛，大一"
    r = recommend(q)
    print(f"\n输入：{q}")
    print(json.dumps({k: v for k, v in r.items() if k != "slots"}, ensure_ascii=False, indent=2))
    print("槽位:", json.dumps(r["slots"], ensure_ascii=False))
