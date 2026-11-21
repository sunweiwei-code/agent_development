# -*- coding: utf-8 -*-
"""
一条命令跑完的演示路径：老师改档案 → 学生端立刻不一样。

它和 e2e_chain_test.py 的区别：
  测试要断言、要能失败报警；演示要**看得见的对比**——同一个人问同一句话，
  改前推荐 A、改后推荐 B，两张截图摆在一起。
  所以这里不吞掉任何输出，每一步都打印给人看。

顺序刻意这样排：先演示"改前"，再改，再演示"改后"，最后回滚复原。
跑完库是干净的，可以重复跑。

用法（服务需先启动）：
    python scripts/demo_chain.py
"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline  # noqa: E402
import teacher   # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = ROOT / "tests" / "edge-profile"
SHOTS = ROOT / "shots"
BASE = "http://127.0.0.1:8000/"

LAB, AREA = "L04", "农业植保无人机"
ASK = "我想做农业植保无人机"


def alive():
    try:
        return urllib.request.urlopen(BASE, timeout=3).status == 200
    except Exception:
        return False


def top(q):
    r = pipeline.recommend(q)
    return r["form"], [(t["lab_id"], t["name"]) for t in r["top3"]]


def show(tag, form, rows):
    print(f"  {tag}  形态={form}")
    for i, (lid, name) in enumerate(rows, 1):
        print(f"        {i}. {name}（{lid}）")
    if not rows:
        print("        （没有推荐）")


def shot(name, q):
    """视口特意拉高：聊天区是 flex+overflow，矮视口会自动滚到底，
    第一张卡的名字正好被推出画面——而"排第一的是谁"恰恰是这张图要证明的事。"""
    url = f"{BASE}chat?auto={urllib.parse.quote(json.dumps([q], ensure_ascii=False))}"
    out = SHOTS / name
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
                    "--no-default-browser-check", f"--user-data-dir={PROFILE}",
                    "--window-size=1600,1500", "--virtual-time-budget=30000",
                    f"--screenshot={out}", url],
                   capture_output=True, timeout=180)
    print(f"  截图 → {out.relative_to(ROOT)}")


def main():
    if not alive():
        print(f"服务没起：{BASE} 打不开。先跑 python server/app.py")
        return 1

    print(f'\n一句话：「{ASK}」\n')

    print("① 改之前——这个话题库里没人做，推荐里没有无人机协会")
    f1, t1 = top(ASK)
    show("改前", f1, t1)
    shot("demo-before.png", ASK)

    print(f"\n② 老师在教师端把「{AREA}」加进无人机协会的研究方向")
    res = teacher.apply(f"研究方向增加{AREA}", LAB, confirmed=True)
    if not res.get("ok"):
        print(f"  写入失败：{res.get('code')}")
        return 1
    sid = res["snapshot"]
    print(f"  已写入，快照 {sid}")

    try:
        print("\n③ 同一句话再问一次")
        f2, t2 = top(ASK)
        show("改后", f2, t2)
        shot("demo-after.png", ASK)

        hit = any(lid == LAB for lid, _ in t2)
        print(f"\n  {'✅' if hit else '❌'} 无人机协会{'已' if hit else '仍未'}进入推荐")
    finally:
        rb = teacher.rollback(sid)
        print(f"\n④ 回滚复原（逐字一致：{rb.get('identical')}）")

    print(f"\n两张图：shots/demo-before.png 与 shots/demo-after.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
