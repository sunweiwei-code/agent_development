# -*- coding: utf-8 -*-
"""
教师端 UI 回归：三栏比例 / 快照可读性 / 审计可读性 / 修改动画。

为什么单独一套：学生端有 ui_test.py 守着，教师端原本只测了后端闸门，
前端改坏了（比例被挤压、字段吐机器词、动画不触发）没有任何东西会报警。

用法：
    python tests/teacher_ui_test.py
    python tests/teacher_ui_test.py --only anim
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = ROOT / "tests" / "edge-profile"
SHOTS = ROOT / "shots"
BASE = "http://127.0.0.1:8000/teacher"


def dom(query="", budget="20000") -> str:
    args = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={PROFILE}",
            "--window-size=1600,1000", f"--virtual-time-budget={budget}",
            "--dump-dom", BASE + query]
    r = subprocess.run(args, capture_output=True, timeout=180)
    return r.stdout.decode("utf-8", "ignore")


def visible(d: str) -> str:
    """只留可见文本：剥掉 <script>/<style>，去掉标签属性。

    断言"页面上有没有露机器词"必须看渲染结果，不能把脚本里的字面量
    和 data-s="snap-2026..." 这种属性算成用户看得见的内容。
    """
    d = re.sub(r"<script[\s\S]*?</script>", "", d)
    d = re.sub(r"<style[\s\S]*?</style>", "", d)
    d = re.sub(r"<[^>]+>", "\x00", d)      # 标签变分隔符，属性一并消失
    return d


def shot(name, query="", budget="20000"):
    SHOTS.mkdir(exist_ok=True)
    p = SHOTS / name
    args = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={PROFILE}",
            "--window-size=1600,1000", f"--virtual-time-budget={budget}",
            f"--screenshot={p}", BASE + query]
    subprocess.run(args, capture_output=True, timeout=180)
    return p


# ---------------- 断言 ----------------

def t_ratio(d):
    """三栏必须是 18 : 32 : 50，不能因为 minmax 下限被挤成 21:37:42。"""
    m = re.search(r'data-measure="([^"]+)"', d)
    if not m:
        return False, "没拿到测量值（?measure=1 钩子失效）"
    raw = m.group(1).replace("&quot;", '"')
    pct = json.loads(raw)["pct"]
    ok = all(abs(a - b) <= 1.5 for a, b in zip(pct, [18, 32, 50]))
    return ok, f"实测 {pct}（期望 [18, 32, 50]）"


def t_snap(d):
    """快照卡片要说人话：有实验室名和改前→改后，不能露 snap-2026xxxx 这种内部 id。"""
    cards = re.findall(r'<div class="snap[^"]*"[^>]*>([\s\S]*?)</div>\s*</div>', d)
    if not cards:
        return False, "一张快照卡片都没有"
    txt = visible(d)
    has_name = bool(re.search(r'(实验室|协会)', txt))
    has_when = bool(re.search(r'\d\d-\d\d \d\d:\d\d', txt))
    leak = bool(re.search(r'snap-\d{8}', txt)) or bool(re.search(r'\bL0\d\b', txt))
    ok = has_name and has_when and not leak
    return ok, f"卡片 {len(cards)} 张，实验室名={has_name} 时间={has_when} 泄漏内部id={leak}"


def t_audit(d):
    """审计记录要显示实验室名和「老师/系统」，不能是 L01 · system。"""
    body = d.split('id="auditBody"')[-1]
    txt = visible(body)
    has_lab = "实验室" in txt or "协会" in txt
    machine = bool(re.search(r'\bL0\d\b', txt)) or "system" in txt or "teacher" in txt
    ok = has_lab and not machine
    return ok, f"含实验室名={has_lab} 残留机器词={machine}"


def t_anim(d):
    """确认写入后必须出现：蓝色「正在修改」+ 内联「改前 → 改后」。"""
    flash = 'class="fld flash"' in d
    diff = 'class="fld-diff"' in d
    badge = "正在修改" in d
    ok = flash and diff and badge
    return ok, f"高亮={flash} 内联diff={diff} 角标={badge}"


def _section(d, tag_id):
    """截取某个元素到下一个同级块之前的片段，避免全页误判。"""
    i = d.find(f'id="{tag_id}"')
    return d[max(0, i - 200): i + 1200] if i >= 0 else ""


def _open_tag(d, tag_id):
    """取该 id 所属元素的完整开标签（从 '<' 到 '>'）。

    不能对切片直接 split('>')：切片起点在标签之前，第一个 '>' 往往属于前一个元素。
    """
    i = d.find(f'id="{tag_id}"')
    if i < 0:
        return ""
    lt = d.rfind("<", 0, i)
    gt = d.find(">", i)
    return d[lt: gt + 1] if lt >= 0 and gt >= 0 else ""


def t_pick(d):
    """选中实验室：输入框里有 chip、搜索栏收起、中栏标题跟着变。"""
    tag = _open_tag(d, "pkChip")
    has_chip = bool(tag) and "hidden" not in tag
    name_in_chip = "RoboMaster" in _section(d, "pkChip")
    search_hidden = bool(re.search(r'id="pkSearch"[^>]*hidden', d))
    title = re.search(r'id="midTitle"[^>]*>([^<]*)<', d)
    mid_ok = bool(title and "RoboMaster" in title.group(1))
    ok = has_chip and name_in_chip and search_hidden and mid_ok
    return ok, f"chip={has_chip} 名称={name_in_chip} 搜索收起={search_hidden} 中栏标题={mid_ok}"


def t_clear(d):
    """叉掉后：chip 消失、搜索栏回来、候选能按关键词过滤。"""
    tag = _open_tag(d, "pkChip")
    chip_hidden = (not tag) or ("hidden" in tag)
    search_back = not re.search(r'id="pkSearch"[^>]*hidden', d)
    menu = _section(d, "pkMenu")
    filtered = "ACM" in menu and "无人机" not in menu
    empty_mid = "选一个实验室" in d
    ok = chip_hidden and search_back and filtered and empty_mid
    return ok, f"chip消失={chip_hidden} 搜索回来={search_back} 过滤生效={filtered} 中栏空态={empty_mid}"


SCEN = [
    ("ratio",  "?measure=1", t_ratio, None),
    ("snap",   "", t_snap, "teacher-ui-snap.png"),
    ("audit",  "?open=audit", t_audit, "teacher-ui-audit.png"),
    ("pick",   "?auto=pick&lab=L01", t_pick, "teacher-ui-pick.png"),
    ("clear",  "?auto=pick&lab=L01&clear=1&q=acm", t_clear, "teacher-ui-clear.png"),
    ("anim",   "?auto=edit&lab=L01&hold=1", t_anim, "teacher-ui-anim.png"),
]


def main():
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    print(f"\n{'场景':<8}{'结果':<6}说明")
    print("-" * 70)
    bad = 0
    for name, q, fn, png in SCEN:
        if only and name != only:
            continue
        d = dom(q)
        ok, why = fn(d)
        if png:
            shot(png, q)
            why += f"  → shots/{png}"
        print(f"{name:<8}{'✅' if ok else '❌':<6}{why}")
        bad += (not ok)
    print("-" * 70)
    total = len([s for s in SCEN if not only or s[0] == only])
    print(f"教师端 UI：{total - bad} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
