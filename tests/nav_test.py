# -*- coding: utf-8 -*-
"""
通道拓扑回归：主页 / 学生端 / 教师端 / 档案页，谁能去哪、谁不能去哪。

规则（产品定，不是实现细节）：
    主页 ↔ 教师端 ✅   主页 ↔ 学生端 ✅   主页 ↔ 档案页 ✅
    学生端 ↔ 档案页 ✅  教师端 ↔ 档案页 ✅
    学生端 ↔ 教师端 ❌

为什么要单独一套：导航是"改一行就破"的东西。少一个入口，用户找不到路；
多一条链接，老师顺着它滑进学生界面。这两种错都不会报错，只会让人用起来别扭，
所以拿测试钉住——尤其要钉"不能"的那一半。

用法：
    python tests/nav_test.py
    python tests/nav_test.py --only 档案
"""
import re
import subprocess
import sys
import urllib.parse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = ROOT / "tests" / "edge-profile"
BASE = "http://127.0.0.1:8000"


def dom(path, budget="15000", limit=180) -> str:
    if "hop=1" in path:      # 这一场要真的跳一次页面（含一次 LLM 调用），时间和预算都给足
        budget, limit = "45000", 300
    args = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={PROFILE}",
            "--window-size=1440,900", f"--virtual-time-budget={budget}",
            "--dump-dom", BASE + path]
    r = subprocess.run(args, capture_output=True, timeout=limit)
    return r.stdout.decode("utf-8", "ignore")


def markup(d: str) -> str:
    """剥脚本与样式。模板字符串里的 href 不该算成真实链接。"""
    d = re.sub(r"<script[\s\S]*?</script>", "", d)
    return re.sub(r"<style[\s\S]*?</style>", "", d)


def hrefs(d: str) -> list:
    return re.findall(r'href="([^"]*)"', markup(d))


def back_of(d: str):
    """档案页左上返回键的指向（由 JS 按 from 决定）。"""
    m = re.search(r'class="back" id="back" href="([^"]*)"', markup(d))
    if m:
        return m.group(1)
    m = re.search(r'id="back"[^>]*href="([^"]*)"', markup(d))
    return m.group(1) if m else None


def back_txt(d: str):
    m = re.search(r'id="backTxt">([^<]*)<', markup(d))
    return m.group(1).strip() if m else None


# ---------- 场景 ----------

def t_home(d):
    """主页是门厅：三个入口都要在，且只有这三个。"""
    hs = hrefs(d)
    has = {k: (k in hs) for k in ("/chat", "/teacher", "/lab")}
    ok = all(has.values())
    return ok, "入口 " + " ".join(f"{k}={'✓' if v else '✗'}" for k, v in has.items())


def t_student(d):
    """学生端：能回主页、能进档案页；绝不能出现教师端入口。"""
    hs = hrefs(d)
    home = "/" in hs
    lab = any(h.startswith("/lab") and "from=student" in h for h in hs)
    leak = any("teacher" in h for h in hs)
    ok = home and lab and not leak
    return ok, f"主页={home} 档案页={lab} 串到教师端={leak}"


def t_student_card(d):
    """推荐卡片上的档案链接要带 from=student，档案页才知道该回哪儿。"""
    hs = [h for h in hrefs(d) if h.startswith("/lab?id=")]
    ok = bool(hs) and all("from=student" in h for h in hs)
    return ok, f"卡片链接 {len(hs)} 个，都带来源={ok}"


def t_teacher(d):
    """教师端：能回主页、能进档案页；绝不能出现学生端入口。"""
    hs = hrefs(d)
    home = "/" in hs
    lab = any(h.startswith("/lab") and "from=teacher" in h for h in hs)
    leak = any(h.startswith("/chat") for h in hs)
    ok = home and lab and not leak
    return ok, f"主页={home} 档案页={lab} 串到学生端={leak}"


def t_lab_from_teacher(d):
    """从教师端进来：返回键回教师端（那边也有对话区，写"回到对话"），且不给学生入口。"""
    b, t = back_of(d), back_txt(d)
    leak = any(h.startswith("/chat") for h in hrefs(d))
    ok = b == "/teacher" and t == "回到对话" and not leak
    return ok, f"返回={b}（{t}） 学生入口={leak}"


def t_lab_from_student(d):
    """从学生端进来：只留一个跨页出口（回对话），不再多摆一个主页按钮。"""
    b, t = back_of(d), back_txt(d)
    extra = "/" in hrefs(d)
    ok = b == "/chat" and t == "回到对话" and not extra
    return ok, f"返回={b}（{t}） 多余主页键={extra}"


def t_lab_direct(d):
    """直接打开（从主页点进来）：返回键就是主页，别处不再放第二个主页按钮。"""
    b, t = back_of(d), back_txt(d)
    dup = hrefs(d).count("/") > 1
    ok = b == "/" and t == "主页" and not dup
    return ok, f"返回={b}（{t}） 重复主页键={dup}"


def t_lab_keep_from(d):
    """档案库里的行链接要继续带着来源，连点两级也不丢上下文。"""
    hs = [h for h in hrefs(d) if h.startswith("/lab?id=")]
    ok = bool(hs) and all("from=teacher" in h for h in hs)
    return ok, f"行链接 {len(hs)} 个，都带来源={ok}"


def t_lab_single_exit(d):
    """档案页只连一条外部通道：回到来时那一端。

    三个入口（/ 、/chat 、/teacher）里只准出现一个——多一个链接不会报错，
    只会让人在顶栏上多犹豫一秒；"全部实验室"是同一页的列表态，不算跨页。
    """
    hs = hrefs(d)
    exits = sorted({h for h in hs if h in ("/", "/chat", "/teacher")
                    or h.startswith("/chat?") or h.startswith("/teacher?")})
    ok = len(exits) == 1
    return ok, f"跨页出口={exits}"


def t_hop(d):
    """从对话点开档案再回来，这段对话还在。

    不留这段对话，学生从档案页点"回到对话"会看到空页面，以为刚才白说了——
    入口做得再顺，回来丢了内容也一样不友好。
    """
    body = re.search(r"<body[^>]*>", d).group(0)
    hop = 'data-hop="restored"' in body
    greeted = 'data-greeted="1"' in body
    ok = hop and not greeted
    return ok, f"对话已恢复={hop} 又弹欢迎语={greeted}"


HOP_AUTO = urllib.parse.quote(json.dumps(["我想做视觉算法，想打比赛"], ensure_ascii=False))
HOP_SID = "s-navhop00000001"

SCEN = [
    ("主页", "/", t_home, None),
    ("学生端", "/chat?fresh=1", t_student, None),
    ("学生卡片", "/chat?auto=" + "%5B%22%E6%88%91%E6%83%B3%E5%81%9A%E8%A7%86%E8%A7%89%E7%AE%97%E6%B3%95%22%5D&fresh=1",
     t_student_card, None),
    ("教师端", "/teacher", t_teacher, None),
    ("档案-教师来源", "/lab?id=L04&from=teacher", t_lab_from_teacher, None),
    ("档案-学生来源", "/lab?id=L04&from=student", t_lab_from_student, None),
    ("档案-直接打开", "/lab?id=L04", t_lab_direct, None),
    ("档案库-保来源", "/lab?from=teacher", t_lab_keep_from, None),
    ("档案-单一出口", "/lab?id=L04&from=student", t_lab_single_exit, None),
    ("对话续存", f"/chat?sid={HOP_SID}&hop=1&auto={HOP_AUTO}", t_hop, None),
]


def main():
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    print(f"\n{'场景':<16}{'结果':<6}说明")
    print("-" * 72)
    bad = 0
    for name, path, fn, _ in SCEN:
        if only and only not in name:
            continue
        try:
            d = dom(path)
            ok, why = fn(d)
        except Exception as e:
            ok, why = False, f"异常 {type(e).__name__}: {e}"
        bad += (not ok)
        print(f"{name:<16}{'✅' if ok else '❌':<6}{why}")

    (ROOT / "data" / "students" / f"{HOP_SID}.json").unlink(missing_ok=True)

    total = len([1 for n, _, _, _ in SCEN if not only or only in n])
    print(f"\n通道测试：{total - bad} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
