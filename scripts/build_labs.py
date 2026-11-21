# -*- coding: utf-8 -*-
"""
M0 步骤 ③④：从 MinerU 的 content_list.json 切分实验室并抽取 L1 字段。

对应 PRD V1.9：
- 5.1 管线第 ③ 步（按实验室切分，组数必须等于目录基准数）、第 ④ 步（抽 L1 字段）
- 5.2 数据分层：L1 真实层字段 + contact 对象结构
- 5.1「为什么用 content_list.json」：保留 page_idx 以便回指原 PDF

用法：
    python scripts/build_labs.py

产出：
    data/labs/labs_l1.json   仅 L1 真实字段（不含任何仿真注入）
    data/labs/parse_report.json  缺失/异常清单，含 page_idx
"""
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "parsed" / "content_list.json"
OUT = ROOT / "data" / "labs" / "labs_l1.json"
REPORT = ROOT / "data" / "labs" / "parse_report.json"

# 只保留 text 块：header / footer / page_number / ref_text / page_footnote 由 MinerU 已分类
# 但注意：MinerU 会把"页脚位置的正文"（QQ 群、邮箱、助理微信）也标成 footer / page_footnote，
# 若按类型一刀切会丢掉真实联系方式。因此改为「按内容过滤噪声，不按类型切」。
NOISE_PATTERNS = [
    "唯实求精", "匠心致远", "SCHOOL OF ARTIFICIAL", "Introduction to the Faculty",
    "学院实验室介绍", "CONTENTS", "SEEK TRUTH", "PURSUE EXCELLENCE",
    "SHENZHEN", "TECHNOLOGY", "UNIVERSITY",
]
PAGE_NUM_RE = re.compile(r"^\d{1,2}$")


def is_noise(t: str) -> bool:
    if PAGE_NUM_RE.match(t):
        return True
    return any(p in t for p in NOISE_PATTERNS)

# PDF 中的重复条目：L40 与 L05 为同一实验室（负责人、intro 完全相同，仅地点与名称后缀不同）。
# 保留两条记录以维持 PDF 原文追溯（各自有 page_idx/bbox），但 L40 不进推荐池、不接受教师绑定。
DUP_OF = {"L40": "L05"}

MARKERS = ("地点", "负责人", "研究方向", "基本介绍", "实验室简介", "实验室概况",
           "团队介绍", "主要成果", "实验室特色")
INTRO_MARKERS = ("基本介绍", "实验室简介", "实验室概况", "团队介绍", "基本情况")
# 小节标题词：负责人后面若紧跟这些词，说明换节了，不能当成负责人续行
SECTION_WORDS = re.compile(r"(简介|介绍|成果|方向|概况|团队|情况|特色)")


CJK = r"\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
PUNCT_MAP = {",": "，", ":": "：", ";": "；", "(": "（", ")": "）", "?": "？", "!": "！"}


def fix_cjk_punct(t: str) -> str:
    """只把「夹在两个中日韩字符之间」的半角标点改为全角。

    限定两侧都是 CJK，是为了不误改 'C1-201,205'（地址）与 'RoboMaster2024(广东站)'
    以外的英文/数字场景。这是 OCR 清洗，不是改写原文措辞。
    """
    for half, full in PUNCT_MAP.items():
        # 允许标点两侧有空格（OCR 常见「7人, 助理教授」），转换时一并去掉多余空格
        t = re.sub(rf"(?<=[{CJK}])\s*\{half}\s*(?=[{CJK}])", full, t)
    # 左括号后紧跟中文、或中文后紧跟右括号的常见情形
    t = re.sub(rf"(?<=[{CJK}])\((?=[{CJK}])", "（", t)
    t = re.sub(rf"(?<=[{CJK}])\)(?=[{CJK}，。；：])", "）", t)
    # 括号配平：出现全角左括号后，其对应的半角右括号也转全角，避免「（… ) 」半全角混用
    if "（" in t:
        out, depth = [], 0
        for ch in t:
            if ch == "（":
                depth += 1
                out.append(ch)
            elif ch == ")" and depth > 0:
                depth -= 1
                out.append("）")
            else:
                out.append(ch)
        t = "".join(out)
    return t


def clean(t: str) -> str:
    """去掉 MinerU/OCR 带出的转义与多余空白。

    注意：**这里不做 NFKC 归一化**。NFKC 会把中文全角标点转成半角，
    存储后展示与教师端 diff 会变成「研究机构,实验室」这种怪样子。
    NFKC 只在 8.2 的「比对规范化」里用，属比较层，不属存储层。
    """
    t = t or ""
    t = re.sub(r"^[-\\\s]+", "", t)          # 行首的 "- " "\- "
    t = re.sub(r"[ \t\u3000]+", " ", t)      # 折叠水平空白，保留换行语义
    t = fix_cjk_punct(t)
    return t.strip()


def normalize_directions(dirs):
    """把「研究方向」小节拆成（短标签, 说明）。

    这份 PDF 的研究方向有两种写法：
      A. 纯标签：`运动机械结构的设计；嵌入式控制算法、导航算法开发…`
      B. 标题 + 长说明：`开源AI制药研究` + `我们利用开源人工智能技术赋能药物研发…`
      C. 标签与说明同块：`迁移学习与模型推理优化：探索在数据分布偏移的情况下…`

    若把说明也塞进 research_areas，会出现 100~165 字的"方向"，方向匹配（子串/同义词/
    n-gram）会被长文本稀释，展示也没法读。因此：
      - 短标签 → research_areas（≤35 字、非句末标点）
      - 长说明 → direction_notes（保留原文，不丢内容）
    """
    areas, notes = [], []

    def add_area(x):
        x = x.strip(" ：:，,。;；")
        if x and x not in areas:
            areas.append(x)

    for raw in dirs:
        # V3.12：除分号/换行外，中文逗号也要拆——实测 12 条实验室的方向是
        # 「脑机接口，物联网工程应用，动物行为与健康分析」这种逗号串联，不拆会导致
        # 整段作为一个 target 参与匹配，粒度失真。英文逗号同为列表分隔符（如
        # "Web安全, AI安全"），但只在无括号包裹时拆，避免拆散 "(a)…；(b)…" 结构。
        raw = re.sub(r"等$", "", raw.strip())
        # 整段说明文字（长且有句号）不参与拆分，直接归说明——L36 的"研究方向"下面
        # 是一段散文，若按逗号切会碎成十几片
        if len(raw) > 40 and "。" in raw:
            notes.append(raw)
            continue
        for part in re.split(r"[;；\n]+", raw):
            p = part.strip(" ，,。;；")
            if not p:
                continue
            # 逗号串联的短清单（各片段 ≤18 字）才拆；否则视为句子，保持完整
            _pieces = [x.strip() for x in re.split(r"[，,、]", p) if x.strip()]
            if len(_pieces) >= 2 and all(len(x) <= 18 for x in _pieces):
                for x in _pieces:
                    add_area(x) if (len(x) <= 18 and not x.endswith("。")) else notes.append(x)
                continue
            m = re.match(r"^(.{2,30}?)[：:](.+)$", p)      # 形式 C
            if m:
                add_area(m.group(1))
                desc = m.group(2).strip()
                if len(desc) >= 10:
                    notes.append(desc)
                continue
            # 形式 D：纯顿号分隔的清单，如「多模态大模型、情感计算、AIGC、计算机视觉…」
            # 限定「不含逗号」：中文逗号常作分句用，出现逗号多半是说明句而不是清单
            # （反例：L34「…软件缺陷预测、定位，可以帮助软件测试人员…」曾被误拆成 2 个方向）
            if "。" not in p and "，" not in p and "、" in p:
                pieces = [x.strip() for x in re.split(r"[、]", p) if x.strip()]
                if len(pieces) >= 2 and all(len(x) <= 30 for x in pieces):
                    for x in pieces:
                        add_area(x)
                    continue
            if len(p) <= 35 and not p.endswith(("。", ".")):   # 形式 A / B 的标题行
                add_area(p)
            else:
                notes.append(p)
    # 兜底：若一条方向都没识别出（方向清单整段用逗号串联，被前面的规则判成说明），
    # 且首段本身是「短片段 + 分隔符」的形态，则退回按分隔符拆开，避免 research_areas 为空。
    if not areas and notes:
        pieces = [x.strip() for x in re.split(r"[、，,]", notes[0]) if x.strip()]
        if len(pieces) >= 2 and all(len(x) <= 25 for x in pieces):
            for x in pieces:
                add_area(x)
            notes = notes[1:]
    return areas, notes


def strip_label(t: str, label: str) -> str:
    return re.sub(rf"^{label}\s*[:：]?\s*", "", t).strip()


def starts_marker(t: str) -> bool:
    return any(t.startswith(m) for m in MARKERS)


def load_blocks():
    """按内容过滤噪声，保留所有类型（footer 里可能有真实联系方式）。"""
    raw = json.loads(SRC.read_text(encoding="utf-8"))
    out = []
    for b in raw:
        if b.get("type") not in ("text", "footer", "page_footnote", "ref_text"):
            continue
        t = clean(b.get("text") or "")
        if not t or is_noise(t):
            continue
        out.append({
            "text": t,
            "page": int(b.get("page_idx", 0)) + 1,
            "level": b.get("text_level"),
            "bbox": b.get("bbox"),
            "type": b.get("type"),
        })
    return out


def split_by_location(blocks):
    """以「地点」为锚点切分：每个实验室必有 1 个地点块（实测 45/45）。

    实验室名 = 地点块之前最近的、不是标记行的块。
    """
    anchors = [i for i, b in enumerate(blocks) if b["text"].startswith("地点")]
    groups = []
    for k, ai in enumerate(anchors):
        # 向前找实验室名
        ni = None
        for j in range(ai - 1, max(-1, ai - 4), -1):
            t = blocks[j]["text"]
            if starts_marker(t) or t.startswith("QQ") or "@" in t or "微信" in t:
                continue
            if len(t) > 60:          # 太长不像名字
                continue
            ni = j
            break
        if ni is None:
            continue
        end = anchors[k + 1] if k + 1 < len(anchors) else len(blocks)
        # 下一组的名可能在本组尾部，先粗切，后面用名字边界修正
        groups.append({"name_idx": ni, "start": ni, "end": end, "page": blocks[ni]["page"]})

    # 修正：本组 end 不应包含下一组的实验室名与其后的 地点/负责人
    for g in groups[:-1]:
        pass
    for idx in range(len(groups) - 1):
        groups[idx]["end"] = groups[idx + 1]["name_idx"]
    return groups


CONTACT_PATTERNS = {
    "qq_group": [r"QQ\s*群\s*[:：]?\s*([0-9]{5,12})", r"Q\s*群\s*[:：]?\s*([0-9]{5,12})"],
    "email": [r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"],
    "wechat": [r"(?:助理)?微信\s*[:：]?\s*([A-Za-z0-9_-]{3,30})"],
    "phone": [r"(?:电话|手机)\s*[:：]?\s*(1[3-9]\d{9})"],
}


def parse_contact(texts):
    """返回 contact 对象（PRD 5.2：{qq_group, email, wechat}，可缺失）。"""
    blob = "\n".join(texts)
    out = {}
    for key, pats in CONTACT_PATTERNS.items():
        for p in pats:
            m = re.search(p, blob, re.I)
            if m:
                out[key] = m.group(1)
                break
    return out


GROUP_PATTERNS = [
    r"分为[^。；]{0,20}组别[^。；]{0,10}(?:分别)?是[^。；]{0,80}",
    r"分成[^。；]{0,20}(?:个)?组[^。；]{0,10}(?:分别)?是[^。；]{0,80}",
]


def parse_group_tags(intro: str):
    """仅在原文明确出现「分为…组别…是」时才抽取，避免误判（PRD 5.2 双归属：有则 L1）。"""
    for p in GROUP_PATTERNS:
        m = re.search(p, intro)
        if not m:
            continue
        seg = m.group(0)
        # 先切到"分别是"之后，避免把"分成四个组别"里的"组"也当成组名
        # （L01 曾因此产出 ["分成四个组", "别分别是机械组", "电控组", ...] 这种碎片）
        body = re.split(r"分别是|分别为", seg)[-1]
        tags = re.findall(r"([\u4e00-\u9fa5A-Za-z]{2,6}组)", body)
        tags = [t for t in tags if t not in ("组别", "小组", "个组", "四个组")]
        # 去重保序
        seen, out = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t)
                out.append(t)
        if out:
            return out
    return []


CONTACT_HINT = re.compile(r"QQ\s*群|Q\s*群|微信|@|电话|手机|邮箱", re.I)
# 只把「标签+冒号」或「纯邮箱」当成联系方式行整行剔除。
# 不能用宽松的 ^手机 匹配：L09 的研究方向是「手机应用开发、游戏开发…」，会被误杀。
CONTACT_LINE = re.compile(
    r"^(QQ\s*群|Q\s*群|微信|助理微信|电话|手机号?|邮箱|邮件|E-?mail)\s*[:：]", re.I)
EMAIL_ONLY = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


PHONE_RE = re.compile(r"1[3-9]\d{9}")


def is_contact_line(t: str) -> bool:
    if CONTACT_LINE.match(t):
        return True
    if EMAIL_ONLY.fullmatch(t.strip()):
        return True
    # 无标签的"手机号 + 邮箱"混排（L45 的 achievements 里混进了一条
    # "13502890355 wuqingyang@sztu.edu.cn"，因没有"电话/邮箱"标签而漏过）
    if PHONE_RE.search(t) and len(t) <= 80 and not re.search(r"[\u4e00-\u9fff]{4,}", t):
        return True
    if re.fullmatch(r"(QQ|Q)\s*群?\s*[:：]?\s*\d{5,12}", t.strip(), re.I):
        return True
    return False


def cx(b):
    """块的横向中心（bbox = [x0,y0,x1,y1]，PDF 坐标系）。"""
    box = b.get("bbox") or [0, 0, 0, 0]
    return (box[0] + box[2]) / 2


def assign_contacts(blocks, groups):
    """联系方式按「同页 + 横向中心最近」强制归属。

    背景：MinerU 把页脚块统一排在该页正文之后，所以一个实验室的联系方式在序号上会落进
    *下一个* 实验室的范围里（p3 的 QQ群:184592069 落在 ACM 段内，实际属于 RoboMaster）。
    双栏排版下按 x 中心就近归属是可靠的：左栏≈350，右栏≈600。

    返回 (owner, by_group)：
      owner[idx] = 该 contact 块归属的组号
      by_group[gi] = 属于该组的 contact 文本列表
    """
    # 每组在每一页的横向中心
    centers = []
    for g in groups:
        seg = blocks[g["start"]: g["end"]]
        per_page = {}
        for b in seg:
            per_page.setdefault(b["page"], []).append(cx(b))
        centers.append({p: sum(v) / len(v) for p, v in per_page.items()})

    owner, by_group = {}, {i: [] for i in range(len(groups))}
    for i, b in enumerate(blocks):
        if not CONTACT_HINT.search(b["text"]):
            continue
        page = b["page"]
        best, bestd = None, 1e9
        for gi, cmap in enumerate(centers):
            if page not in cmap:
                continue
            d = abs(cmap[page] - cx(b))
            if d < bestd:
                best, bestd = gi, d
        if best is None:
            continue
        owner[i] = best
        by_group[best].append(b["text"])
    return owner, by_group


def build():
    blocks = load_blocks()
    groups = split_by_location(blocks)
    owner, contact_by_group = assign_contacts(blocks, groups)
    labs, report = [], []
    for gi, g in enumerate(groups):
        i = gi + 1
        seg = blocks[g["start"]: g["end"]]
        # 正文文本：排除已判给别人的联系块
        texts = [b["text"] for k, b in enumerate(seg, start=g["start"])
                 if not (k in owner and owner[k] != gi)]
        name = texts[0]

        # 分字段
        loc = teacher = None
        dirs, intro_parts, ach_parts = [], [], []
        phase = None
        seen_dirs = False
        for b in seg[1:]:
            t = b["text"]
            if t.startswith("地点"):
                loc = strip_label(t, "地点"); phase = "loc"; continue
            if t.startswith("负责人"):
                teacher = strip_label(t, "负责人"); phase = "teacher"; continue
            if t.startswith("研究方向"):
                # 只有第一个「研究方向」进入 dirs 阶段。L11 在同一实验室内有第二段
                # 「研究方向」说明（逐方向展开的长描述），若再次切换 dirs 会把长段落
                # 塞进 research_areas 并从 intro 中丢失。第二段按 intro 子节处理。
                if not seen_dirs:
                    seen_dirs = True
                    phase = "dirs"
                elif not ach_parts:
                    phase = "intro"
                continue
            if t.startswith("基本介绍") or any(t.startswith(m) for m in INTRO_MARKERS) \
                    or t.startswith("实验室特色"):
                phase = "intro"; continue
            if t.startswith("主要成果"):
                phase = "ach"; continue
            # 联系方式行不入正文
            if is_contact_line(t):
                continue
            if phase == "teacher" and teacher is not None and len(t) < 30 \
                    and not SECTION_WORDS.search(t):
                teacher = (teacher + " " + t).strip()   # 负责人换行被拆开的情况
                continue
            if phase == "dirs":
                dirs.append(t)
            elif phase == "intro":
                intro_parts.append(t)
            elif phase == "ach":
                ach_parts.append(t)

        contact = parse_contact(texts + contact_by_group[gi])
        intro = " ".join(intro_parts).strip()
        areas, dir_notes = normalize_directions(dirs)
        labs.append({
            "lab_id": f"L{i:02d}",
            "name": name,
            "location": loc,
            "teacher": teacher,
            "research_areas": areas,
            "direction_notes": "\n".join(dir_notes) if dir_notes else None,
            "intro": intro,
            "achievements": ach_parts,
            "contact": contact or None,
            "group_tags": parse_group_tags(intro),
            "source_pages": sorted({b["page"] for b in seg}),
            "duplicate_of": DUP_OF.get(f"L{i:02d}"),
        })

        missing = [k for k, v in         {
            "location": loc, "teacher": teacher,
            "research_areas": areas, "intro": intro, "achievements": ach_parts,
        }.items() if not v]        # direction_notes 为可选字段，不计入缺失
        if missing:
            report.append({"lab_id": f"L{i:02d}", "name": name,
                           "page_idx": g["page"], "missing": missing})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(labs, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT.write_text(json.dumps({"lab_count": len(labs), "issues": report},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    return labs, report


if __name__ == "__main__":
    labs, report = build()
    print(f"切分组数: {len(labs)}")
    n_dir = sum(1 for l in labs if l["research_areas"])
    n_intro = sum(1 for l in labs if l["intro"])
    n_ach = sum(1 for l in labs if l["achievements"])
    n_ct = sum(1 for l in labs if l["contact"])
    n_loc = sum(1 for l in labs if l["location"])
    n_tea = sum(1 for l in labs if l["teacher"])
    n_grp = sum(1 for l in labs if l["group_tags"])
    print(f"location {n_loc}/{len(labs)} | teacher {n_tea} | research_areas {n_dir} "
          f"| intro {n_intro} | achievements {n_ach} | contact {n_ct} | group_tags {n_grp}")
    print("\n--- 缺失明细 ---")
    for r in report:
        print(f'{r["lab_id"]} p{r["page_idx"]:>2} {r["name"][:26]:28s} 缺: {",".join(r["missing"])}')
