# -*- coding: utf-8 -*-
"""
M0 步骤：L2 仿真注入 + field_sources 覆盖（PRD 5.2 / 5.3）。

注入范围（V2.6 定）：
  1. recruit_info —— 45 条全部 L2 注入。
     理由：扫过全文，只有 5 条出现泛泛"欢迎大家加入"，**没有任何一条给出招新时间或年级要求**，
     即 recruit_info 的三个子键都无原文依据。若把 note 标 l1、window 标 l2，
     字段级标记会说不清；因此整体标 l2_simulated，原文的欢迎语本来就留在 intro 里。
  2. updated_at —— R6 时间漂移：11 条标 2023-06（早于 KB_BASE_DATE-12 个月 → 触发过期提示）。

**不注入**：
  - group_tags：PDF 只有 1 条写了组别，其余 44 条保持空缺。按 R1"以真实数据为准、不人为删改"，
    不编造组别；这 44 条正好用来测"缺失字段兜底展示（组别：暂无）"。
  - intros：一字不改（5.3 边界规则）。

R2 四种招新说法（评审定：三种均分 + 随机）：
  yes 招收大一新生 / likely 欢迎低年级同学参与 / senior_only 以大二以上为主 / unclear 完全不提

R5 内部冲突：**不制造**。5.3 的 R5 原写法是"intro 写以竞赛为主、achievements 全是论文"，
  但 intro 按边界规则一字不改，制造冲突就必须改写 L1，与 5.1/5.2 冲突。
  改为使用**天然存在的双类共存样本**：L13/L16/L34/L39（竞赛项与科研项同时存在且比值远离 1:1），
  这本身就是"要解释为什么以 A 为主却也有 B"的冲突处理场景。

产出：data/labs/labs.json（最终 M0 数据集，含 field_sources / confidence / data_source）
"""
import json
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
L1 = ROOT / "data" / "labs" / "labs_l1.json"
PT = ROOT / "data" / "labs" / "path_type.json"
OUT = ROOT / "data" / "labs" / "labs.json"

SEED = 20261118
KB_BASE_DATE = "2024-12"

# R2 四种说法 → recruit_info
R2_VARIANTS = {
    "yes": {"window": "9月", "accepts_freshman": "yes",
            "note": "每年 9 月统一招新，明确招收大一新生"},
    "likely": {"window": "9月—10月", "accepts_freshman": "likely",
               "note": "欢迎低年级同学参与，大一起可先跟组学习"},
    "senior_only": {"window": "9月", "accepts_freshman": "senior_only",
                    "note": "以大二以上为主，需先完成基础课程"},
    "unclear": {"window": None, "accepts_freshman": "unclear", "note": None},
}

# R6 时间漂移：选 confidence 偏低或信息偏薄的记录，让"旧"与"不全"相关，更像真实数据
STALE_LABS = ["L17", "L23", "L25", "L26", "L30", "L32", "L33",
              "L41", "L42", "L43", "L45"]

# 天然双类共存样本（R5 的替代：不制造冲突，用天然样本）
CONFLICT_SAMPLES = ["L13", "L16", "L34", "L39"]

L1_FIELDS = ["location", "teacher", "research_areas", "intro",
             "achievements", "direction_notes", "group_tags", "contact"]


def main():
    labs = json.loads(L1.read_text(encoding="utf-8"))
    pt = json.loads(PT.read_text(encoding="utf-8"))

    # R2 分配：三种说法均分，剩余给 unclear；用固定种子保证可复现
    rnd = random.Random(SEED)
    # 重复条目与其主记录保持一致（L40 跟随 L05），避免同一实验室两套招新说法
    owners = [l for l in labs if not l["duplicate_of"]]
    n = len(owners)
    base = n // 4
    pool = (["yes"] * base) + (["likely"] * base) + (["senior_only"] * base)
    pool += ["unclear"] * (n - len(pool))
    rnd.shuffle(pool)
    assign = {l["lab_id"]: pool[i] for i, l in enumerate(owners)}
    for l in labs:
        if l["duplicate_of"]:
            assign[l["lab_id"]] = assign.get(l["duplicate_of"], "unclear")

    out = []
    for lab in labs:
        lid = lab["lab_id"]
        rec = dict(lab)
        rec["recruit_info"] = R2_VARIANTS[assign[lid]]
        rec["updated_at"] = "2023-06" if lid in STALE_LABS else KB_BASE_DATE
        rec["path_type"] = pt[lid]["path_type"]
        rec["confidence"] = pt[lid]["confidence"]

        fs = {}
        for f in L1_FIELDS:
            v = rec.get(f)
            if v:                                  # 只给非空字段标来源
                fs[f] = "l1"
        rec["name"] = rec["name"]                  # name 也是 l1
        fs["name"] = "l1"
        fs["path_type"] = "l2_simulated"
        fs["recruit_info"] = "l2_simulated"
        fs["updated_at"] = "l2_simulated" if rec["updated_at"] == "2023-06" else "l1"
        rec["field_sources"] = fs
        rec["data_source"] = "mixed" if any(v != "l1" for v in fs.values()) else "real"
        rec["conflict_sample"] = lid in CONFLICT_SAMPLES
        out.append(rec)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- 验收报告 ----------------
    print(f"记录数 {len(out)}（不同实验室 {sum(1 for x in out if not x['duplicate_of'])}）")
    print("R2 分布:", dict(Counter(x["recruit_info"]["accepts_freshman"] for x in out)))
    print("updated_at 分布:", dict(Counter(x["updated_at"] for x in out)))
    cover = [len([f for f, v in x.items() if v]) for x in out]
    print("\nfield_sources 覆盖检查：")
    miss = []
    for x in out:
        declared = set(x["field_sources"])
        for f in L1_FIELDS + ["name"]:
            if x.get(f) and f not in declared:
                miss.append((x["lab_id"], f, "非空但未标来源"))
        if not x["field_sources"]:
            miss.append((x["lab_id"], "-", "field_sources 为空"))
    print("  漏标:", miss if miss else "无（非空字段 100% 有来源标记）")
    print("\ndata_source:", dict(Counter(x["data_source"] for x in out)))
    print("confidence:", dict(Counter(x["confidence"] for x in out)))
    print("conflict_sample:", [x["lab_id"] for x in out if x["conflict_sample"]])


if __name__ == "__main__":
    main()
