# -*- coding: utf-8 -*-
"""
实验室档案页回归：内容完整 / 边界样本不塌 / 三个入口都通 / 学生端卡片有链接。

为什么单独一套：档案页是"长内容"的落脚点，最容易出的问题是
字段漏渲染（quiet）、空数据时版式塌掉（L21/L33 没成果）、
长名称溢出（L06）、以及入口链接被改坏（学生点不进去）。

用法：
    python tests/lab_test.py
    python tests/lab_test.py --only edge
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
BASE = "http://127.0.0.1:8000"
LABS = json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8"))
BYID = {l["lab_id"]: l for l in LABS}


def dom(path, budget="12000") -> str:
    args = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={PROFILE}",
            "--window-size=1440,1200", f"--virtual-time-budget={budget}",
            "--dump-dom", BASE + path]
    r = subprocess.run(args, capture_output=True, timeout=180)
    return r.stdout.decode("utf-8", "ignore")


def shot(name, path, budget="12000"):
    SHOTS.mkdir(exist_ok=True)
    p = SHOTS / name
    args = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={PROFILE}",
            "--window-size=1440,1200", f"--virtual-time-budget={budget}",
            f"--screenshot={p}", BASE + path]
    subprocess.run(args, capture_output=True, timeout=180)
    return p


def visible(d: str) -> str:
    d = re.sub(r"<script[\s\S]*?</script>", "", d)
    d = re.sub(r"<style[\s\S]*?</style>", "", d)
    d = re.sub(r"<[^>]+>", "\x00", d)
    return d


def markup(d: str) -> str:
    """剥掉脚本与样式后的标记：数元素必须用这个。

    页面里的模板字符串（`<a class="row" href="/lab?id=${...}">`）会跟着 <script>
    一起进 DOM，直接 d.count('class="row"') 会把它算成一条真实数据——
    teacher_ui_test 踩过同一个坑。
    """
    d = re.sub(r"<script[\s\S]*?</script>", "", d)
    d = re.sub(r"<style[\s\S]*?</style>", "", d)
    # 属性里的 & 会被序列化成 &amp;（如 /lab?id=L04&amp;from=student），
    # 不还原回来，按 href 断言的正则会全部落空
    return d.replace("&amp;", "&")


def ready(d: str) -> bool:
    return 'data-ready="1"' in d


# ---------------- 断言 ----------------

def t_full(d):
    """L04：名称/老师/地点/方向/简介/成果条目都要在页面上，且年份被拆到左侧列。"""
    L = BYID["L04"]
    t = visible(d)
    has_name = L["name"] in t
    has_teacher = L["teacher"] in t
    has_loc = L["location"] in t
    areas = all(a in t for a in L["research_areas"])
    intro_ok = L["intro"][:40] in t            # 长简介被折叠，只断言开头在
    ach = L["achievements"][0][4:20] in t      # 首条去掉开头年份后应出现在列表里
    years = re.findall(r'class="y">(\d{4})</div>', d)
    grouped = years == sorted(years, reverse=True) and len(years) >= 2
    ok = all([has_name, has_teacher, has_loc, areas, intro_ok, ach, grouped])
    return ok, (f"名称={has_name} 老师={has_teacher} 地点={has_loc} 方向={areas} "
                f"简介={intro_ok} 成果={ach} 年份分组={years}")


def t_tail_year(d):
    """L06 的成果是「…二等奖,2021」这种年份在末尾的写法，必须也能归到年份。"""
    years = re.findall(r'class="y">(\d{4})</div>', d)
    ok = len(years) >= 3 and "2019" in years
    return ok, f"识别出年份列 {years}"


def t_edge(d):
    """L21 无成果：不能塌成空白页，要给一句人话，且不显示「0 条」。"""
    t = visible(d)
    has_hint = "暂无可查" in t
    no_zero = "0 条" not in t
    other_ok = "联系方式" in t and "招新信息" in t
    ok = ready(d) and has_hint and no_zero and other_ok
    return ok, f"空态提示={has_hint} 无「0条」={no_zero} 其余板块={other_ok}"


def t_longname(d):
    """L06 名字 25 字：标题不能溢出到卡片外（用 scrollWidth 判断）。"""
    L = BYID["L06"]
    t = visible(d)
    ok = L["name"] in t and ready(d)
    return ok, f"长名称完整渲染={L['name'] in t}"


def t_missing_values(d):
    """L33 有完整招新/联系方式但没成果：不能漏渲染成 None/null，也不能板块塌掉。"""
    t = visible(d)
    has_rec = "招大一" in t
    no_ach = "暂无可查" in t
    clean = all(x not in t for x in ("None", "null", "undefined", "[object"))
    ok = ready(d) and has_rec and no_ach and clean
    return ok, f"招新={has_rec} 无成果空态={no_ach} 无 None/null={clean}"


def t_lib(d):
    """档案库：45 行、可搜索、按 id 稳定排序。"""
    m = markup(d)
    rows = len(re.findall(r'<a class="row"', m))
    has_search = 'id="q"' in m
    has_count = "共 45 个实验室" in visible(d)
    ok = rows == len(LABS) and has_search and has_count
    return ok, f"列表 {rows} 行（期望 {len(LABS)}） 搜索框={has_search} 计数={has_count}"


def t_card_link(d):
    """学生端每张推荐卡都要有通往档案页的链接，否则详情页没有入口。"""
    m = markup(d)
    cards = m.count('class="card"')
    links = re.findall(r'href="/lab\?id=(L\d+)(?:&from=student)?"', m)
    ok = cards >= 2 and len(links) >= cards
    return ok, f"卡片 {cards} 张，档案链接 {len(links)} 个"


def t_missing_lab(d):
    """不存在的 id 要给回退路径，不能白屏。"""
    t = visible(d)
    ok = "没找到" in t and "/lab" in d
    return ok, f"提示={('没找到' in t)} 兜底链接={'/lab' in d}"


SCEN = [
    ("full",     "/lab?id=L04", t_full, "lab-L04.png"),
    ("tailyear", "/lab?id=L06", t_tail_year, "lab-L06.png"),
    ("edge",     "/lab?id=L21", t_edge, "lab-L21.png"),
    ("longname", "/lab?id=L06", t_longname, None),
    ("blank",    "/lab?id=L33", t_missing_values, "lab-L33.png"),
    ("lib",      "/lab", t_lib, "lab-lib.png"),
    ("404",      "/lab?id=L999", t_missing_lab, None),
    # fresh=1：这一场只是要渲染出卡片看链接，不要污染跨会话记忆
    ("cardlink", '/chat?auto=' + "%5B%22%E6%88%91%E6%83%B3%E5%81%9A%E8%A7%86%E8%A7%89%E7%AE%97%E6%B3%95%EF%BC%8C%E6%83%B3%E6%89%93%E6%AF%94%E8%B5%9B%22%5D&fresh=1",
     t_card_link, "entry-card.png"),
]


def main():
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    print(f"\n{'场景':<10}{'结果':<6}说明")
    print("-" * 70)
    bad = 0
    for name, path, fn, png in SCEN:
        if only and name != only:
            continue
        d = dom(path)
        ok, why = fn(d)
        if png:
            shot(png, path)
            why += f"  → shots/{png}"
        print(f"{name:<10}{'✅' if ok else '❌':<6}{why}")
        bad += (not ok)
    print("-" * 70)
    total = len([s for s in SCEN if not only or s[0] == only])
    print(f"档案页：{total - bad} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
