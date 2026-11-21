# -*- coding: utf-8 -*-
"""
推荐理由生成 + 名称幻觉校验（G-02）。

为什么这两件事必须一起做：
  理由生成是全链路里**唯一可能引入编造的环节**。Top3 的实验室名直接取自 labs.json，
  结构上不可能错；一旦让 LLM 自由写文案，它就可能写出库外机构名或编造研究方向。
  而 G-02 是硬指标（幻觉率必须为 0，一次都不允许）。

所以：生成 → 白名单校验 → 不合格重试一次 → 仍不合格退回模板（降级不阻断）。

PRD 对应：
  G-02 名称幻觉率 = 0；"输出"范围含 Top3、others、推荐理由
  8.1 理由要基于 breakdown 说人话，不是复述分数
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import chat_json, chat_stream

ROOT = Path(__file__).resolve().parents[1]

# 机构名识别。字符类必须覆盖名称里常见的《》〈〉「」·—，否则
# "深技大-浙江大华《计算机视觉与人工智能》联合实验室"会在《处断开，
# 只截出"联合实验室"三个字，反而被判成库外名（实测踩到）。
ORG_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()《》〈〉「」『』\-—·\s]{2,40}?"
    r"(?:实验室|研究中心|研究院|中心|俱乐部|协会|基地|英才班|战队)"
)
GENERIC = {"该实验室", "本实验室", "实验室", "这类实验室", "相关实验室",
           "其他实验室", "研究中心", "中心", "研究院"}
# 泛指词可能被"粘"进更长的捕获片段里，例如
#   "你的计算成像兴趣与科研目标同该实验室" —— 结尾是"实验室"，但整句是泛指
# 只要片段里出现这些指代词，就当泛指处理，不算机构名。
GENERIC_INFIX = ("该实验室", "本实验室", "这个实验室", "这家实验室",
                 "上述实验室", "这些实验室", "这类实验室", "相关实验室", "同该实验室")


_ALL_AREAS = None


def _all_areas():
    global _ALL_AREAS
    if _ALL_AREAS is None:
        labs = json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8"))
        _ALL_AREAS = [a for l in labs for a in (l.get("research_areas") or [])]
    return _ALL_AREAS


def load_whitelist():
    labs = json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8"))
    return {l["lab_id"]: l["name"] for l in labs}


def _norm(s):
    """规范化：去掉括号内容与所有标点/空白，只留中文与字母数字。

    必须去干净——白名单名里带《》和 -（如"深技大-浙江大华《计算机视觉与人工智能》联合实验室"），
    模型若写成"计算机视觉与人工智能联合实验室"，不去标点就比对不上，会误判为幻觉。
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s)


def _norm(s):
    """规范化：去掉括号内容与所有标点/空白，只留中文与字母数字。

    必须去干净——白名单名里带《》和 -（如"深技大-浙江大华《计算机视觉与人工智能》联合实验室"），
    模型若写成"计算机视觉与人工智能联合实验室"，不去标点就比对不上，会误判为幻觉。
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s)


def _iter_suffixes(n, max_drop=10):
    """逐个去掉开头的字，产出候选名后缀。

    为什么不用"引导词表"剥离：引导词是开放集合——"推荐/另外/此外/比较推荐/首先/还有/顺便说"…
    实测用词表只能覆盖一部分，"另外计算机视觉与人工智能联合实验室"仍被判成幻觉。
    改为**后缀逐位匹配**：只要存在某个去掉前 N 个字的后缀能对上白名单，就不算编造。
    """
    for i in range(0, min(max_drop, len(n) - 2)):
        yield n[i:]


def _match_whitelist(name, wl_norm):
    """判断候选名是否指向白名单内的实验室。

    只允许"首尾锚定"的匹配，不允许任意子串：
      · sub == 白名单名                    → 全等
      · 白名单名.endswith(sub)             → 模型省略了前缀（"计算机视觉与人工智能联合实验室"
                                              是全名的尾段）
      · 白名单名.startswith(sub)           → 模型省略了后缀
    曾经用"任意子串"更宽松，结果「机器人创新实验室」因为含"创新实验室"、
    命中了「计算机学生科技创新实验室」而被放过——过松比过严危险得多。
    """
    n = _norm(name)
    if not n:
        return True
    for sub in _iter_suffixes(n):
        if len(sub) < 4:
            break
        for w in wl_norm:
            if sub == w:
                return True
            # 首尾截断必须"截得不多"：保留部分 ≥ 白名单名的 60%。
            # 否则「机器人创新实验室」会因尾段"创新实验室"命中
            # 「计算机学生科技创新实验室」而被放过（实测踩到）。
            if len(sub) >= 0.6 * len(w) and (w.endswith(sub) or w.startswith(sub)):
                return True
    return False


def verify_names(text, whitelist):
    """返回文本中出现的**库外机构名**列表。空列表 = 干净。"""
    wl_norm = {_norm(v) for v in whitelist.values()}
    bad = []
    for m in ORG_RE.finditer(text or ""):
        name = m.group(0).strip()
        if name in GENERIC or len(name) <= 3:
            continue
        if any(g in name for g in GENERIC_INFIX):
            continue
        if _match_whitelist(name, wl_norm):
            continue
        bad.append(name)
    return bad


# ============ 方向一致性校验（防"内容幻觉"）============
# 名字幻觉用户一眼能看出，但"理由里编一个该室没有的方向"更难发现——
# 学生并不知道那个实验室到底做什么。所以要在提示词之外加一道规则校验。
#
# 误报是这里最大的敌人：第一版把"做/方向"后面的一切都当方向，
# 结果"低年级也能进""都挺对口""走科研路"全被标成幻觉（实测 14 处误报）。
# 两道收敛：
#   ① 捕获字符集限定为中文/字母/数字/顿号与连接词，遇到逗号句号就停
#   ② 只对"像方向词"的片段判定——必须在全局方向词表里，或以技术后缀结尾
DIR_TRIGGER = re.compile(
    r"(?:主要做|方向是|方向|围绕|涉及|聚焦|专注|侧重|研究|做)"
    r"([\u4e00-\u9fa5A-Za-z0-9、和与及以及]{2,40})"
)
SPLIT = re.compile(r"[、]|以及|和|与|及")
TECH_SUFFIX = ("算法", "模型", "计算", "识别", "检测", "感知", "分析", "学习", "优化",
               "重建", "定位", "控制", "通信", "芯片", "机器人", "大模型", "视觉",
               "图像", "网络", "系统", "平台", "生成", "处理", "设计", "开发")
GENERIC_DIR = {
    "方向", "技术", "应用", "研究", "开发", "系统", "工程", "问题", "能力", "基础",
    "内容", "领域", "数据", "项目", "竞赛", "科研", "工作", "学习", "实践", "训练",
    "培养", "算法", "模型", "平台", "方案", "团队", "老师", "学生", "成果", "设备",
}


def _global_terms(all_areas):
    """全局方向词表：把所有实验室的方向拆成短语，用于识别"用了别室的方向"。"""
    terms = set()
    for a in all_areas:
        for p_ in re.split(r"[、，,]|以及|和|与", a or ""):
            p_ = p_.strip()
            if 3 <= len(p_) <= 20:
                terms.add(p_)
    return terms


def verify_directions(text, lab_areas, all_areas, tol=0.6):
    """返回理由中**该室没有的方向表述**列表。

    放宽三层（避免把正常改述判成幻觉）：
      1. 与任一 research_area 互为子串 → 放过
      2. 与该室方向的簇内匹配分 ≥ tol（exact/near/adjacent 都算）→ 放过
      3. 不像方向词（不在全局词表、也不以技术后缀结尾）→ 放过
    只有三者都不满足才标记。
    """
    from score import op_match_detail
    areas = [a for a in (lab_areas or []) if a]
    gterms = _global_terms(all_areas)
    bad = []
    for m in DIR_TRIGGER.finditer(re.sub(r"[\s　]", "", text or "")):
        for chunk in SPLIT.split(m.group(1)):
            c = chunk.strip("。；！？的 　")
            if len(c) < 3 or c in GENERIC_DIR:
                continue
            if not (c in gterms or any(c.endswith(sfx) for sfx in TECH_SUFFIX)):
                continue                      # 不像方向词，放过（放宽 3）
            if any(c in a or a in c for a in areas):
                continue                      # 放宽 1
            if areas and op_match_detail(c, areas)[0] >= tol:
                continue                      # 放宽 2
            bad.append(c)
    return bad


SYS = """你在为深圳技术大学的学生推荐实验室。任务：为给定的 3 个实验室各写一段推荐理由。

硬性要求：
1. 理由里**不必重复实验室名称**（界面上名称单独展示）；若确实提到，**必须原文照抄**，一个字都不改。
2. **只允许提及给定的这 3 个实验室**，绝不许提到其他任何实验室、中心、研究院、俱乐部。
3. 每段 2 句话、60 字以内：第一句说"为什么匹配你"（结合学生说的方向/诉求），第二句说"这个实验室是做什么的"（只能用给定的研究方向，不许编造）。
4. 不要写分数、不要写"匹配度 X%"、不要写成评价口吻。
5. 语气像学长介绍，不要广告腔（不要"顶尖""一流""不可错过"）。

只输出 JSON：{"<lab_id>": "<理由>", ...}"""


def build_user_msg(top3, slots):
    items = []
    for t in top3:
        items.append({
            "lab_id": t["lab_id"],
            "name": t["name"],                    # 原文，模型必须照抄
            "research_areas": t.get("research_areas", []),
            "path_type": t.get("path_type"),
            "why_matched": t.get("why_matched", []),   # 规则给出的匹配要点，模型据此说人话
        })
    return json.dumps({
        "student": {k: v for k, v in slots.items() if v},
        "labs": items,
    }, ensure_ascii=False, indent=1)


SYS_ONE = """你在为深圳技术大学的学生写**一个**实验室的推荐理由。

硬性要求：
1. 60 字以内、2 句话：第一句说"为什么匹配你"（结合学生诉求），第二句说"这个实验室做什么"（只能用给定研究方向，不许编造）。
2. 不要重复实验室名称；不提其他任何实验室、中心、研究院、俱乐部。
3. 不写分数、不写"匹配度"，不用广告词（顶尖/一流/不可错过）。
4. 直接输出理由正文，不要任何前缀、不要引号、不要 markdown。

只输出这一段的正文。"""


def build_one_msg(item, slots):
    return json.dumps({
        "student": {k: v for k, v in (slots or {}).items() if v},
        "lab": {"name": item["name"], "research_areas": item.get("research_areas", []),
                "path_type": item.get("path_type"), "why_matched": item.get("why_matched", [])},
    }, ensure_ascii=False)


def generate_streamed(top3, slots, on_token, whitelist=None):
    """逐实验室**流式**生成理由：边生成边回调，前端可实时显示。

    与 batch 版的差别：多花 2 次调用（每个实验室一次），换来首字可见。
    校验仍在**整段生成完之后**做——名称/方向不合规就把该卡替换成模板，
    并通过 on_token(..., reset=True) 让前端整体替换。
    """
    whitelist = whitelist or load_whitelist()
    all_areas = _all_areas()
    out = {}
    for it in top3:
        msgs = [{"role": "system", "content": SYS_ONE},
                {"role": "user", "content": build_one_msg(it, slots)}]
        on_token(it["lab_id"], "", True)            # reset=True：清空该卡占位，并打上打字光标
        buf = []
        try:
            for d in chat_stream(msgs, max_tokens=180):
                buf.append(d)
                on_token(it["lab_id"], d, False)
        except Exception as e:
            print(f"[warn] 流式生成失败 {it['lab_id']}: {e}", file=sys.stderr)

        text = "".join(buf).strip().strip('"“”')
        bad = verify_names(text, whitelist) if text else ["(空)"]
        dwarn = verify_directions(text, it.get("research_areas"), all_areas) if text else []
        if bad or dwarn or not text:
            fb = it.get("reason", "")
            on_token(it["lab_id"], fb, True)         # 用模板整体替换
            out[it["lab_id"]] = {"text": fb, "violations": bad,
                                 "dir_warnings": dwarn, "fallback": True}
        else:
            out[it["lab_id"]] = {"text": text, "violations": [],
                                 "dir_warnings": [], "fallback": False}
    return out


def generate(top3, slots, whitelist=None):
    """批量生成理由（一次调用写 3 段）。

    返回 {lab_id: {"text","violations","dir_warnings","fallback"}}
      violations   : 库外机构名（G-02，硬拦截 → 重试 → 模板降级）
      dir_warnings : 该室没有的方向（内容幻觉，软标记 → 重试一次，仍不改只记录不阻断）
    """
    whitelist = whitelist or load_whitelist()
    def _check(lab, text):
        return (verify_names(text, whitelist),
                verify_directions(text, lab.get("research_areas"), _all_areas()))
    out = {}
    try:
        data = chat_json([
            {"role": "system", "content": SYS},
            {"role": "user", "content": build_user_msg(top3, slots)},
        ], max_tokens=900)
    except Exception as e:
        print(f"[warn] 理由生成失败，回退模板：{e}", file=sys.stderr)
        data = {}

    for t in top3:
        text = (data.get(t["lab_id"]) or "").strip()
        if not text:
            out[t["lab_id"]] = {"text": t.get("reason", ""), "violations": [],
                                "dir_warnings": [], "fallback": True}
            continue
        bad, dwarn = _check(t, text)
        if not bad and not dwarn:
            out[t["lab_id"]] = {"text": text, "violations": [], "dir_warnings": [], "fallback": False}
            continue
        # 有问题就重试一次（名称问题与方向问题都要说清）
        try:
            hint = []
            if bad:
                hint.append(f"出现了不允许的机构名 {bad}")
            if dwarn:
                hint.append(f"提到了该实验室没有的方向 {dwarn}，只能用给定 research_areas 里的方向")
            retry = chat_json([
                {"role": "system", "content": SYS + "\n注意：上一版有问题——" + "；".join(hint) + "。这次必须修正。"},
                {"role": "user", "content": build_user_msg(top3, slots)},
            ], max_tokens=900)
            text2 = (retry.get(t["lab_id"]) or "").strip()
            if text2:
                bad2, dwarn2 = _check(t, text2)
                if not bad2:
                    # 名称干净就采用（方向若仍有警告，软标记但不阻断）
                    out[t["lab_id"]] = {"text": text2, "violations": [],
                                        "dir_warnings": dwarn2, "fallback": False}
                    continue
        except Exception:
            pass
        out[t["lab_id"]] = {"text": t.get("reason", ""), "violations": bad,
                            "dir_warnings": [], "fallback": True}
        continue
        bad = verify_names(text, whitelist)
        if bad:
            # 重试一次：更严格的提示
            try:
                retry = chat_json([
                    {"role": "system", "content": SYS + "\n注意：上一版出现了不允许的机构名 "
                                                       f"{bad}，这次只能出现给定的 3 个名称。"},
                    {"role": "user", "content": build_user_msg(top3, slots)},
                ], max_tokens=900)
                text2 = (retry.get(t["lab_id"]) or "").strip()
                if text2 and not verify_names(text2, whitelist):
                    out[t["lab_id"]] = {"text": text2, "violations": [], "fallback": False}
                    continue
            except Exception:
                pass
            out[t["lab_id"]] = {"text": t.get("reason", ""), "violations": bad, "fallback": True}
        else:
            out[t["lab_id"]] = {"text": text, "violations": [], "fallback": False}
    return out


if __name__ == "__main__":
    wl = load_whitelist()
    demo = [{"lab_id": "L01", "name": "RoboMaster悍匠实验室",
             "research_areas": ["运动机械结构的设计", "嵌入式控制算法", "导航算法开发",
                                "视觉识别算法和雷达算法的开发"],
             "path_type": "竞赛主导",
             "why_matched": ["方向匹配（视觉识别算法）", "竞赛主导", "有 RoboMaster 目标赛事"]},
            {"lab_id": "L29", "name": "多源感知及智能计算实验室",
             "research_areas": ["多模态智能计算", "垂直大模型", "机器视觉"],
             "path_type": "双主导", "why_matched": ["方向匹配（机器视觉）", "双主导"]}]
    r = generate(demo, {"interest": "视觉算法", "goal": "竞赛", "grade": "大一"}, wl)
    for k, v in r.items():
        print(f"\n[{k}] fallback={v['fallback']} violations={v['violations']}\n{v['text']}")
    # 校验器反测试：既要不放过库外名，也不能误报库内名（假阳性会白白降级成模板）
    print("\n=== 校验器反测试 ===")
    cases = [
        ("无名称", "这里正好有视觉识别算法方向", 0),
        ("库内全名裸写", "深技大-浙江大华《计算机视觉与人工智能》联合实验室", 0),
        ("库内全名+书名号", "深技大-浙江大华《计算机视觉与人工智能》联合实验室做计算机视觉", 0),
        ("引导词+库内简称", "另外计算机视觉与人工智能联合实验室也不错", 0),
        ("引导词2+库内简称", "比较推荐计算机视觉与人工智能联合实验室", 0),
        ("泛指", "该实验室方向对口", 0),
        ("引导词+库外名", "推荐机器人视觉实验室", 1),
        ("标点+库外名", "此外，机器人创新实验室也很好", 1),
        ("库外全名", "另外清华大学智能感知研究中心也值得看", 1),
        ("库外相似名", "物联网智慧云实验室", 1),
    ]
    ok = True
    for label, text, expect in cases:
        bad = verify_names(text, wl)
        hit = len(bad) == expect
        ok &= hit
        print(f'  {"✅" if hit else "❌"} {label}: 期望{expect} 实得{len(bad)} {bad}')
    print("\n校验器:", "有效" if ok else "有漏洞，需修")
