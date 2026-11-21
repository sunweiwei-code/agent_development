# -*- coding: utf-8 -*-
"""
PRD 8.1 / 8.1.1 打分模型的参考实现（M0 标定与后续 M1 共用）。

严格按文档实现，不加"均多项"式的自创门槛：
  方向  0.35 × 算子(interest, research_areas)
  路径  0.25 × 映射表(goal, path_type)
  技术栈 0.20 × 算子(tech_stack, intro+research_areas+group_tags+direction_notes)
  年级  0.10 × (可入 +0.10 / 缺失或 unclear 0 / 明确不收 -0.15)
  教师比赛 0.10 × (命中 1 否则 0；teacher 为 null 记 0)
  再 × confidence 权重(high/medium 1.0, low 0.85)

缺失维度记 0，**不重新归一化**。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABS = ROOT / "data" / "labs" / "labs.json"

W = {"direction": 0.35, "path": 0.25, "tech": 0.20, "grade": 0.10, "pref": 0.10}

# 同义词组（8.1.1 要求覆盖库内高频方向）
# ============ 语义簇（V3.12 重构：三档强弱，替代原来的"同组即等价"） ============
# 为什么改：原来 _syn_hit 是"两组各含同组任一词即等价"，把三种关系压成同一个 0.8——
#   ① 同一核心概念（医疗机器人 ↔ 智能手术机器人）应 1.0
#   ② 相邻子领域（医疗机器人 ↔ 具身智能）应 0.6
#   ③ 共享泛词但领域不同（视觉算法 ↔ 医学图像处理）应 0
# 结构：每个簇分三档。exact=1.0（同一概念的不同说法）/ near=0.8（同类方法）/ adjacent=0.6（相邻）
CLUSTERS = {
    "视觉": {
        "exact": ["视觉算法", "视觉识别算法", "视觉"],
        "near": ["计算机视觉", "机器视觉", "图像识别", "目标检测", "三维感知", "点云"],
        "adjacent": [],
    },
    "边缘计算": {
        "exact": ["边缘计算"],
        "near": ["边缘智能"],
        "adjacent": ["嵌入式", "嵌入式控制算法", "单片机"],
    },
    "数据分析": {
        "exact": ["数据分析", "大数据", "数据库", "数据科学"],
        "near": ["数据挖掘", "机器学习", "深度学习", "智能数据", "信号处理", "数据前沿"],
        "adjacent": ["数据安全", "信息安全", "统计分析"],
    },
    "机器人": {
        "exact": ["医疗机器人", "机器人"],
        "near": ["机械臂", "移动机器人"],
        "adjacent": ["具身智能", "机器狗", "无人系统", "无人机"],
    },
    "网络安全": {
        "exact": ["网络安全", "信息安全"],
        "near": ["数据安全", "网络攻防", "密码技术", "电子取证", "AI安全"],
        "adjacent": [],
    },
    "物联网": {
        "exact": ["物联网"],
        "near": ["传感网", "无线传感", "IoT"],
        "adjacent": ["嵌入式", "边缘计算"],
    },
    "通信": {
        "exact": ["通信", "通信网络", "通信算法"],
        "near": ["无线通信", "射频", "信号处理", "滤波器"],
        "adjacent": [],
    },
    "芯片": {
        "exact": ["芯片", "集成电路"],
        "near": ["半导体", "传感器芯片", "微电子", "传感芯片"],
        "adjacent": [],
    },
    "云计算": {
        "exact": ["云计算", "云原生"],
        "near": ["分布式系统", "微服务", "容器"],
        "adjacent": [],
    },
    "大模型": {
        "exact": ["大模型", "大语言模型", "LLM"],
        "near": ["多模态大模型", "垂直大模型", "生成式"],
        "adjacent": ["自然语言处理"],
    },
    "医学影像": {
        "exact": ["医学影像", "医学图像", "医疗影像"],
        "near": ["医学图像处理", "生物医学", "医学信号"],
        "adjacent": ["健康数据", "临床"],
    },
    "人工智能": {
        # AI 是总称，AIGC 是它的子领域：不是"无关"，而是"更具体"，所以给 adjacent 而非 0
        "exact": ["人工智能", "AI"],
        "near": ["深度学习", "机器学习", "大模型"],
        "adjacent": ["AIGC", "生成式", "多模态大模型"],
    },
    "鸿蒙": {
        "exact": ["鸿蒙", "HarmonyOS", "OpenHarmony"],
        "near": ["国产操作系统"],
        "adjacent": [],
    },
}

# 领域限定词：查询里出现这些词时，目标必须命中同域词，否则直接 0（修"领域词不起作用"）
DOMAIN_WORDS = {
    "医疗": ["医疗", "医学", "手术", "临床", "健康", "康复", "制药", "病理"],
    "生物": ["生物", "基因", "蛋白质", "细胞", "抗体"],
    "金融": ["金融", "证券", "风控", "交易"],
    "工业": ["工业", "制造", "产线", "装备"],
    "农业": ["农业", "种植", "作物"],
    "交通": ["交通", "车路", "自动驾驶", "车联网"],
    "教育": ["教育", "教学", "学习分析"],
}

# 近似同义对：只有这些组合给 1.0。其余"同簇 exact"只给 0.8——
# 因为"视觉算法 vs 计算机视觉"是包含关系（CV 包含视觉算法），不是同义。
EQUIV_PAIRS = [
    ("数据分析", "大数据"), ("数据分析", "数据库"), ("数据分析", "数据科学"),
    ("数据分析", "大数据技术"), ("数据分析", "大数据前沿"),
    ("边缘计算", "边缘计算"),
    ("医疗机器人", "手术机器人"), ("医疗机器人", "医疗机器人"),
]

# 查询词归一化：同一概念的英文缩写/中文全称必须等价，否则"AI"与"人工智能"会给出不同排序。
# 例：学生说"AI"时，"AI辅助制药"靠子串拿 1.0 排第一；说"人工智能"时，
# 「人工智能技术研发与应用」才子串命中——同一诉求两种结果，不可接受。
QUERY_ALIAS = {"AI": "人工智能", "ai": "人工智能", "Ai": "人工智能"}

# 这些词整体含领域词但是**单一概念**，不能拆成"领域+方法"（否则"医疗机器人"会被
# 当成"医疗领域 + 机器人方法"，导致所有医疗器械类实验室被误判为领域不符）
DOMAIN_COMPOUND_SKIP = {"医疗机器人", "医疗影像", "医学影像", "生物信息", "工业机器人", "服务机器人"}

PATH_MAP = {
    "科研": {"科研主导": 1.0, "竞赛主导": 0.1, "就业项目主导": 0.3, "教学平台": 0.2, "双主导": 0.8, "待确认": 0.0},
    "竞赛": {"科研主导": 0.1, "竞赛主导": 1.0, "就业项目主导": 0.4, "教学平台": 0.3, "双主导": 0.8, "待确认": 0.0},
    "就业": {"科研主导": 0.3, "竞赛主导": 0.4, "就业项目主导": 1.0, "教学平台": 0.4, "双主导": 0.8, "待确认": 0.0},
    "未定": {k: 0.5 for k in ("科研主导", "竞赛主导", "就业项目主导", "教学平台", "双主导", "待确认")},
}
PATH_MAP["未定"]["待确认"] = 0.0

CJK = re.compile(r"[\u4e00-\u9fff]")


def _has(text, w):
    """包含判断。纯 ASCII 缩写要求词边界——否则 "AI" 会命中 "AIGC"，
    而真正的中文全称「人工智能技术研发与应用」反而命中不了。"""
    if not w:
        return False
    if w.isascii():
        return re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.I) is not None
    return w in text


def _clusters_of(text):
    """返回 {簇名: 命中档位}，取该文本在各簇中的最高档。"""
    out = {}
    for cname, tiers in CLUSTERS.items():
        for level, words in tiers.items():
            for w in words:
                if _has(text, w):
                    rank = {"exact": 3, "near": 2, "adjacent": 1}[level]
                    if cname not in out or rank > out[cname][0]:
                        out[cname] = (rank, level, w)
    return {k: (v[1], v[2]) for k, v in out.items()}


def _domains_of(text):
    return [d for d, words in DOMAIN_WORDS.items() if any(w in text for w in words)]


def _head_conflict(target, cname):
    """目标里的核心词是否被修饰成另一簇的复合词（如"大数据信息安全"→ 数据被安全修饰）。"""
    tiers = CLUSTERS.get(cname, {})
    for w in tiers.get("exact", []):
        idx = target.find(w)
        if idx < 0:
            continue
        tail = target[idx + len(w): idx + len(w) + 6]
        if tail[:1] in ("，", ",", "、", "；", ";", "。", " "):
            continue          # 逗号/顿号分隔的是并列项，不是复合词
        for other, t2 in CLUSTERS.items():
            if other == cname:
                continue
            if any(o and o in tail for o in t2.get("exact", []) + t2.get("near", [])):
                return True
    return False


def bigrams(s):
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def jaccard(a, b):
    x, y = bigrams(a), bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _tier_score(level):
    return {"exact": 0.8, "near": 0.8, "adjacent": 0.6}.get(level, 0.0)


def _is_equiv(q, t):
    for a, b in EQUIV_PAIRS:
        if (a in q and b in t) or (b in q and a in t):
            return True
    return False


def op_match_detail(interest, targets):
    """方向匹配算子 v2（带归因）。

    结构：先判「领域」再判「方法」，避免"医疗数据分析"与"金融数据分析"命中同一批结果。
    语义：簇内分 exact(1.0) / near(0.8) / adjacent(0.6) 三档，不再"同组即等价"。
    """
    if not interest or not targets:
        return 0.0, "无输入", None
    interest = QUERY_ALIAS.get(interest.strip(), interest)

    q_clusters = _clusters_of(interest)
    q_domains = [] if interest in DOMAIN_COMPOUND_SKIP else _domains_of(interest)
    q_cname = max(q_clusters, key=lambda k: len(q_clusters[k][1])) if q_clusters else None

    best = (0.0, "未命中", None)
    for t in targets:
        # ① 完整子串：1.0（"边缘计算" ⊂ "边缘计算"）
        if _has(t, interest):
            return 1.0, "子串", t

        t_clusters = _clusters_of(t)

        # ② 查询带领域限定 → 目标必须命中同域词，否则直接 0
        if q_domains:
            if not any(w in t for d in q_domains for w in DOMAIN_WORDS[d]):
                # 领域不符：只有当命中的方法词"专属本簇"时保留弱关联 0.6；
                # 若该词同时是别簇的 exact/near（如"数据安全"属网络安全），判 0
                if q_cname and q_cname in t_clusters:
                    lv, mw = t_clusters[q_cname]
                    foreign = any(mw in (w2 for w2 in t2.get("exact", []) + t2.get("near", []))
                                  for c2, t2 in CLUSTERS.items() if c2 != q_cname)
                    if not foreign and lv == "adjacent":
                        cand = (0.6, f"领域不符但方法相邻({mw})", t)
                        if cand[0] > best[0]:
                            best = cand
                continue
            if q_cname and q_cname in t_clusters:
                level = t_clusters[q_cname][0]
                sc = _tier_score(level) if level != "exact" else 1.0
                cand = (sc, f"领域+方法({level})", t)
            else:
                cand = (0.6, "仅领域命中", t)
            if cand[0] > best[0]:
                best = cand
            continue

        # ③ 无领域限定：按查询所属簇的三档取分
        if q_cname and q_cname in t_clusters:
            level, matched_word = t_clusters[q_cname]
            sc = _tier_score(level)
            if level == "exact" and _head_conflict(t, q_cname):
                sc = 0.8                       # 核心词被别簇修饰（大数据信息安全 → 0.8），优先于同义判定
            elif _is_equiv(interest, t):
                sc = 1.0                       # 近乎同义（数据分析↔大数据/数据库）
            cand = (sc, f"簇:{q_cname}/{level}", t)
        else:
            # ④ 兜底：字符级相似（阈值高，避免误配）
            j = max((jaccard(interest, x) for x in targets), default=0.0)
            cand = (0.6, f"2-gram Jaccard={j:.2f}", t) if j >= 0.5 else (0.0, f"未达阈({j:.2f})", t)
        if cand[0] > best[0]:
            best = cand

    return best


def op_match(interest, targets):
    """8.1.1 方向匹配算子：子串 1.0 → 簇内 exact 1.0 / near 0.8 / adjacent 0.6 → 0。"""
    return op_match_detail(interest, targets)[0]


def op_in_text(tech, text):
    """技术栈算子：子串 1.0 → 簇内 exact 0.8 → 字符相似 0.6。长文本用更低阈值。"""
    if not tech:
        return 0.0
    if tech in text:
        return 1.0
    qc = _clusters_of(tech)
    if qc:
        cname = max(qc, key=lambda k: len(qc[k][1]))
        tc = _clusters_of(text)
        if cname in tc and tc[cname][0] in ("exact", "near"):
            return 0.8
    return 0.6 if jaccard(tech, text) >= 0.3 else 0.0


def grade_score(accepts, student_grade=None):
    """年级维度：必须**同时**看实验室招新范围和学生年级。

    旧实现只读实验室的 accepts_freshman，不读学生年级——结果"只招大二以上"
    对大一、大二一视同仁地扣分，等于年级维度失效。
    """
    g = str(student_grade or "")
    is_freshman = g.startswith("大一")
    if accepts in ("yes", "likely"):
        return 1.0
    if accepts == "senior_only":
        # 只在学生确为大一才扣；年级未知或无年级限制时不惩罚
        return -1.5 if is_freshman else 0.0
    return 0.0


def score_lab(profile, lab):
    areas = lab.get("research_areas") or []
    text = " ".join([
        lab.get("intro") or "",
        " ".join(areas),
        " ".join(lab.get("group_tags") or []),
        lab.get("direction_notes") or "",
    ])
    d = op_match(profile.get("interest"), areas)
    # 以赛事命名的实验室，赛事本身即其主攻方向：
    # "RoboMaster悍匠实验室" 的方向列表写的是"运动机械结构/嵌入式控制"，
    # 不含"机器人"字样，但学生说"想打 RoboMaster"时它显然最对口。
    if not d and profile.get("target_contest") and profile["target_contest"] in (lab.get("name") or ""):
        d = 1.0
    p = PATH_MAP.get(profile.get("goal") or "未定", PATH_MAP["未定"]).get(
        (lab.get("path_type") or ["待确认"])[0], 0.0)
    t = op_in_text(profile.get("tech_stack"), text)
    g = grade_score((lab.get("recruit_info") or {}).get("accepts_freshman"),
                    profile.get("grade"))
    pref = 0.0
    if profile.get("preferred_teacher") and lab.get("teacher"):
        if profile["preferred_teacher"] in lab["teacher"]:
            pref = 1.0
    if not pref and profile.get("target_contest"):
        c = profile["target_contest"]
        # 赛事名可能出现在成果里，也可能直接写进实验室名或组别
        # （"RoboMaster悍匠实验室" 的方向列表里没有"机器人"三个字，只能靠名字命中）
        hay = " ".join((lab.get("achievements") or []) + (lab.get("group_tags") or []) + [lab.get("name") or ""])
        if c in hay:
            pref = 1.0
    raw = (W["direction"] * d + W["path"] * p + W["tech"] * t
           + W["grade"] * g + W["pref"] * pref)
    mult = 0.85 if lab.get("confidence") == "low" else 1.0
    return {
        "score": round(raw * mult, 4),
        "direction": d, "path": p, "tech": t, "grade": g, "pref": pref,
        "conf_mult": mult,
    }


def rank(profile, labs):
    rows = [(l, score_lab(profile, l)) for l in labs if not l.get("duplicate_of")]
    rows.sort(key=lambda x: -x[1]["score"])
    return rows


if __name__ == "__main__":
    labs = json.loads(LABS.read_text(encoding="utf-8"))
    demo = {"interest": "视觉算法", "goal": "竞赛", "tech_stack": "Python", "grade": "大一"}
    for lab, s in rank(demo, labs)[:5]:
        print(f'{lab["lab_id"]} {lab["name"][:18]:20s} {s}')
