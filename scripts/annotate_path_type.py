# -*- coding: utf-8 -*-
"""
M0 步骤：path_type 判定（PRD 5.4 两段式规则 + V2.4 计数口径 B）。

口径 B（PRD 5.4）：数**条目内成果项数**，不是 achievement 条目数。
  例 L29：科研 6(论文)+4(专利)+7(软著)=17 项，竞赛 15 项 → 17:15 → 双主导。
  数字只从明确数字取（"20 余项"取 20，"多项"计 1），与 R3（量词模糊不虚构）不冲突。

V2.4 删除：V2.3 实现里额外加的"两类各 ≥2 条"门槛——5.4 原文没有，属执行偏离规则。

V2.4 规则补丁（把 V2.3 的人工覆盖改成规则，避免下一轮再人工特批一遍）：
  P1 achievements 里的"基金/课题"必须带 获批/立项/到账/主持/承担 才计科研项，
     否则只是经费来源（L18"广东省科技创新战略专项基金"曾因此被误计）。
  P2 校企共建/产教融合特征**不再单独**触发就业判定；需有实习/就业/入职/岗位实据，
     或 intro 明确"实习实训/订单培养/就业闭环"（L45 曾因"校企联合实验室"被判就业）。
  P3 教学类表述需与"课程/认证/实训/培养方案"绑定，且 achievements 无奖项条目，
     否则竞赛/科研优先（L14 曾因 ach 里借用的挑战杯金奖被判竞赛）。
  P4 confidence：两类共存的边界记录（比值贴 0.5/2.0 或两类项数 ≤2）上限 medium；
     判定依赖计数口径才成立的记 low。

产出：data/labs/path_type.json
"""
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "labs" / "labs_l1.json"
OUT = ROOT / "data" / "labs" / "path_type.json"

COMP_KW = ["一等奖", "二等奖", "三等奖", "特等奖", "金奖", "银奖", "铜奖", "冠军", "亚军", "季军",
           "奖项", "竞赛", "大赛", "比赛", "赛区", "国赛", "省赛", "联赛", "对抗赛",
           "RoboMaster", "ICPC", "CCPC", "ACM-ICPC", "程序设计竞赛", "蓝桥杯", "挑战杯",
           "互联网+", "数学建模", "RoboCom", "机器人大赛", "创新创业"]
RES_KW = ["论文", "期刊", "SCI", "EI", "专利", "软著", "软件著作权", "著作权", "专著", "教材",
          "基金", "课题"]
JOB_KW = ["实习", "就业", "入职", "工作岗位", "岗位", "订单班"]
JOB_TEXT = re.compile(r"(实习实训|实习基地|就业|订单培养|就业闭环|推荐.{0,6}就业)")
TEACH_KW = ["课程", "实训", "教学", "培养方案", "认证", "教学案例"]
TEACH_TEXT = re.compile(r"(承担.{0,10}教学|承担.{0,10}实训|课程支撑|通识|实验教学|教学平台|云原生班|认证)")
FORM_KW = ["战队", "集训队", "竞赛队", "参赛队"]
# P7：设备/样机/原型等"研制物"属于科研产出（5.4 原文未覆盖，V2.4 补）
DEVICE_KW = ["仪", "设备", "样机", "原型", "装置", "相机", "传感器", "芯片", "机器人", "原型系统"]
FUND_VERB = re.compile(r"(获批|立项|到账|主持|承担|获得).{0,10}(基金|课题)")

NUM_RE = re.compile(r"(\d+)\s*(?:余)?\s*(篇|项|个|次|名)")


def strip_parens(t):
    """去掉括号内补充说明：'（一区 4 篇）'是'6 篇'的子集，不去会重复计数。"""
    return re.sub(r"[（(][^）)]*[）)]", "", t)


def _has_key(ctx, kws):
    return any(k.lower() in ctx.lower() for k in kws)


def _fund_is_output(ctx, tail):
    """P1：裸"基金/课题"不算科研产出，需带获批/立项/到账/主持/承担。"""
    return bool(FUND_VERB.search(ctx + tail))


CLAUSE_SPLIT = re.compile(r"[；;。]")
EN_CITE = re.compile(r"(IEEE|ACM\s|Trans\.|Journal|Conference|vol\.|pp\.|et al)")


def _classify_clause(cl):
    """判断分句属于竞赛/科研/两类都有/都不是。"""
    is_comp = _has_key(cl, COMP_KW)
    is_res = _has_key(cl, RES_KW)
    return is_comp, is_res


def count_items(ach):
    """按口径 B 数条目内成果项数 → (竞赛项数, 科研项数, 计数明细)

    V2.4 改进（V2.3 的窗口法会串句、且无法处理"合计"重复）：
      1) 以分句为单位判类别，不再用 16 字窗口——避免相邻分句的关键词互相污染；
      2) 同一 entry 内出现"合计"分句时，只取合计值，跳过同类明细（L02"一等奖19项+
         国家级奖项合计158项+省级498项"若全累加会得 675，重复计了 19）；
      3) 英文文献引文条目（含 IEEE/Trans./vol. 等）每条计 1 科研项——
         L17 的 achievements 全是英文引文，没有"论文 N 篇"字样，窗口法数出 0。
    """
    comp = res = 0
    detail = []
    for entry in ach:
        text = strip_parens(entry)
        clauses = [c for c in CLAUSE_SPLIT.split(text) if c.strip()]
        if not clauses:
            continue
        # 英文文献条目：整条计 1 科研项
        latin = len(re.findall(r"[A-Za-z]", text))
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        if latin > 30 and cjk <= 6:
            res += 1
            detail.append("科研+1(英文文献)")
            continue
        if EN_CITE.search(text) and not NUM_RE.search(text):
            res += 1
            detail.append("科研+1(引文)")
            continue
        has_total = any("合计" in c for c in clauses)
        for cl in clauses:
            is_comp, is_res = _classify_clause(cl)
            nums = list(NUM_RE.finditer(cl))
            if not nums:
                continue
            if has_total and "合计" not in cl:
                continue          # 有合计值时忽略明细分句，防重复累加
            if is_comp and is_res:
                pos_a = max([cl.rfind(k) for k in ("奖", "赛")] + [-1])
                pos_r = max([cl.rfind(k) for k in ("论文", "专利", "软著")] + [-1])
                tgt = "comp" if pos_a >= pos_r else "res"
            elif is_comp:
                tgt = "comp"
            elif is_res:
                # P1：裸"基金/课题"不是产出
                if ("基金" in cl or "课题" in cl) and not FUND_VERB.search(cl):
                    continue
                tgt = "res"
            else:
                continue
            # 引用量/被引量是影响力指标，不是成果项。
            # 必须按**每个数字的上下文**过滤：L44"会议发表论文100余篇，谷歌学术引用6200余次"
            # 同属一个分句且含"发表"，按分句过滤会漏，6200 会被算成科研 6200 项。
            kept = []
            for m in nums:
                ctx2 = cl[max(0, m.start() - 14): m.start()]
                if re.search(r"(引用|被引|他引|下载)", ctx2) and not re.search(r"(发表|授权|获批|申请)", ctx2):
                    continue
                kept.append(int(m.group(1)))
            if not kept:
                continue
            n = sum(kept)
            if tgt == "comp":
                comp += n
                detail.append(f"竞赛+{n}")
            else:
                res += n
                detail.append(f"科研+{n}")
        # 无数字但有类别关键词
        if not NUM_RE.search(text) and not EN_CITE.search(text):
            is_comp, is_res = _classify_clause(text)
            # P7：无数值、无竞赛词，但是设备/样机清单 → 科研产出（L45 三维感知）
            if not is_comp and not is_res and any(k in text for k in DEVICE_KW):
                res += 1
                detail.append("科研+1(研制物)")
                continue
            if is_comp:
                comp += 1
                detail.append("竞赛+1")
            elif is_res and not (("基金" in text or "课题" in text) and not FUND_VERB.search(text)):
                res += 1
                detail.append("科研+1")
    # 跨条目的「合计」去重：若同一实验室有"奖项合计 N 项"的条目，
    # 则同类的明细分句（如"国家级一等奖19项"）是子集，不能累加（L02 曾得 675）
    totals = [e for e in ach if "合计" in e]
    if totals and NUM_RE.search(" ".join(totals)):
        comp2 = res2 = 0
        for e in totals:
            is_comp, is_res = _classify_clause(e)
            n = sum(int(m.group(1)) for m in NUM_RE.finditer(e))
            if is_comp:
                comp2 += n
            elif is_res:
                res2 += n
        if comp2 or res2:
            detail.append("已按合计去重")
            comp, res = comp2, res2
    return comp, res, detail


def judge(lab):
    ach = lab.get("achievements") or []
    intro = lab.get("intro") or ""
    notes = lab.get("direction_notes") or ""
    blob = intro + " " + notes
    name = lab["name"]

    comp_n, res_n, detail = count_items(ach)
    has_form = any(k in blob for k in FORM_KW)
    intro_sci = bool(re.search(
        r"(国家自然科学基金|国家重点研发|省自然基金|获批.{0,6}基金|发表.{0,6}论文\s*\d+|授权.{0,4}专利\s*\d+)", intro))
    A = (has_form and comp_n >= 1) or comp_n >= 2
    B = res_n >= 1 or intro_sci or ("科研实验室" in name)

    # ---- 第一段：并重（5.4 原文两条判据，无"各≥2"门槛）----
    # 注意"为主"二字：两类都有时，若不是并重，要看谁占优，不能一律落到竞赛。
    # V2.3 的 A 只要求 comp_n>=1，导致 L44（论文 100 余篇 + 1 个奖项）被判竞赛主导。
    explicit_both = bool(re.search(r"(并重|双轮驱动|竞赛与科研)", intro))
    if comp_n and res_n:
        ratio = comp_n / res_n
        if 0.5 <= ratio <= 2.0 or explicit_both:
            near_edge = ratio <= 0.55 or ratio >= 1.8
            if near_edge or min(comp_n, res_n) <= 2:
                conf = "medium"
            else:
                conf = "high"
            # P4：两类依赖同一段文字数出来的，口径敏感 → 不高于 medium
            if "歧义" in "".join(detail):
                conf = "low" if conf == "medium" else "medium"
            return "双主导", conf, {"竞赛项": comp_n, "科研项": res_n,
                                    "比值": round(ratio, 2), "计数明细": ";".join(detail[:5])}
    # ---- 第二段：两类都有但不并重 → 按占优方；只有一类 → 该方 ----
    if comp_n and res_n:
        if comp_n > res_n:
            return "竞赛主导", ("high" if comp_n >= 3 * res_n else "medium"), \
                   {"竞赛项": comp_n, "科研项": res_n, "比值": round(comp_n / res_n, 2)}
        return "科研主导", ("high" if res_n >= 3 * comp_n else "medium"), \
               {"竞赛项": comp_n, "科研项": res_n, "比值": round(comp_n / res_n, 2)}
    if comp_n:                      # 只有竞赛信号（无科研项）
        conf = "high" if comp_n >= 3 or has_form else "medium"
        return "竞赛主导", conf, {"竞赛项": comp_n, "竞赛建制": has_form}
    if res_n:                       # 只有科研信号（无竞赛项）
        conf = "high" if res_n >= 3 else "medium"
        return "科研主导", conf, {"科研项": res_n}

    # B 成立但无量化产出（L32 名称含"科研实验室"、ach 为技术成果描述；L33 无 achievements）
    if B:
        return "科研主导", "low", {"依据": "名称或 intro 表明科研定位，achievements 无量化产出"}

    # P2：校企特征不再单独判就业
    job_n = sum(1 for a in ach if any(k in a for k in JOB_KW)) + (1 if JOB_TEXT.search(blob) else 0)
    if job_n >= 1:
        return "就业项目主导", ("high" if job_n >= 3 else "medium"), {"就业条目": job_n}

    # P3：教学需与课程/认证/实训绑定，且无奖项条目
    teach = bool(TEACH_TEXT.search(intro)) and not comp_n
    if teach or any(k in a for a in ach for k in TEACH_KW):
        return "教学平台", "medium", {"教学表述": teach, "竞赛项": comp_n}
    return "待确认", "low", {"竞赛项": comp_n, "科研项": res_n}


def main():
    labs = json.loads(SRC.read_text(encoding="utf-8"))
    out = {}
    for lab in labs:
        pt, conf, ev = judge(lab)
        out[lab["lab_id"]] = {
            "path_type": [pt], "confidence": conf, "evidence": ev,
            "source": "rule", "duplicate_of": lab.get("duplicate_of"),
        }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("path_type 分布:", dict(Counter(v["path_type"][0] for v in out.values())))
    print("confidence 分布:", dict(Counter(v["confidence"] for v in out.values())))
    for lab in labs:
        v = out[lab["lab_id"]]
        ev = " ".join(f"{a}={b}" for a, b in v["evidence"].items())
        print(f'{lab["lab_id"]} {v["path_type"][0]:6s} {v["confidence"]:6s} | {lab["name"][:24]:26s} | {ev}')


if __name__ == "__main__":
    main()
