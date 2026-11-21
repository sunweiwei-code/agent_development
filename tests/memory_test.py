# -*- coding: utf-8 -*-
"""
跨会话长期记忆（V3.56）：HTTP 层跑，不看实现看行为。

为什么不用无头浏览器：这是"关掉页面再打开还在不在"的问题，浏览器每次跑都是
新 process、Cookie 也跟着变，反而更难复现。这里显式指定 sid，等于模拟
"同一台机器上第二次打开"——Cookie 只是 sid 的搬运工。

重点断言三件事：
  1. 该记住的记住（换会话、不带 history，也能接着说）
  2. 不该串的别串（两个 sid 互不污染）
  3. 说清楚删得掉（clear 之后真的回到空白，且文件没了）

用法： python tests/memory_test.py
"""
import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import memory as mem  # noqa: E402

BASE = "http://127.0.0.1:8000"
STORE = ROOT / "data" / "students"
ALLOWED_KEYS = {"sid", "created", "visits", "turns", "slots", "recent", "last_lab", "last_seen"}


def _req(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chat(sid, text, history=None):
    return _req(f"/api/chat?sid={sid}", {"text": text, "history": history})


def main():
    sid = "s-m" + secrets.token_hex(5)
    other = "s-m" + secrets.token_hex(5)
    rows, ok, bad = [], 0, 0

    def check(name, passed, msg=""):
        nonlocal ok, bad
        rows.append((name, passed, msg))
        ok += bool(passed)
        bad += (not passed)

    # 1 新身份：没有记忆，并下发 Cookie
    m = _req(f"/api/memory?sid={sid}")
    check("新身份无记忆", m.get("exists") is False and not (STORE / f"{sid}.json").exists(),
          f"exists={m.get('exists')}")

    # 2 第一轮：记住方向
    r1 = chat(sid, "我想做视觉算法，想打比赛，大一")
    check("第一轮成型①", r1.get("form") == "①", f"form={r1.get('form')}")
    check("回填画像", "视觉算法" in (r1.get("memory") or {}).get("profile", ""),
          (r1.get("memory") or {}).get("profile"))

    # 3 落盘文件：字段白名单，不多存
    f = STORE / f"{sid}.json"
    disk = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    check("落盘在项目内", f.exists() and f.parent == STORE, str(f))
    check("只存白名单字段", disk and set(disk) <= ALLOWED_KEYS, str(sorted(disk)))
    check("不存'未定'噪音", "未定" not in (disk.get("slots") or {}).values(),
          str(disk.get("slots")))

    # 4 换一次会话：不带 history，说"随便看看"，也要能接着上次的方向推荐
    r2 = chat(sid, "随便看看")
    check("跨会话仍给推荐", r2.get("form") == "①" and len(r2.get("top3") or []) >= 1,
          f"form={r2.get('form')} top3={[x['lab_id'] for x in r2.get('top3') or []]}")

    # 5 对照组：另一个 sid 说同一句话 → 没有方向，应该追问
    r2b = chat(other, "随便看看")
    check("两个学生互不污染", r2b.get("form") == "追问",
          f"form={r2b.get('form')}")

    # 6 跨会话指代：没提名字，"它"也要知道说的是上次第一个
    last = (r2.get("top3") or [{}])[0].get("lab_id")
    r3 = chat(sid, "它的大概情况怎么样")
    check("跨会话指代还原", r3.get("form") == "详情" and (r3.get("lab") or {}).get("lab_id") == last,
          f"form={r3.get('form')} lab={(r3.get('lab') or {}).get('lab_id')} 期望 {last}")

    # 7 换方向：新值覆盖旧值
    chat(sid, "我现在想做数据分析了")
    slots = (_req(f"/api/memory?sid={sid}").get("slots") or {})
    check("方向可被改写", "数据" in str(slots.get("interest")), str(slots.get("interest")))

    # 8 清除：文件消失，回到空白，重新开始追问
    c = _req(f"/api/memory/clear?sid={sid}", {}, method="POST")
    check("清除换新身份", c.get("ok") is True and c.get("sid") not in (None, sid), str(c.get("sid")))
    check("文件已删除", not f.exists(), str(f))
    r4 = chat(sid, "随便看看")
    check("清除后回到空白", r4.get("form") == "追问", f"form={r4.get('form')}")

    # 9 过期：91 天没动过的文件读不出来，且顺手删掉
    old_sid = "s-m" + secrets.token_hex(5)
    p = STORE / f"{old_sid}.json"
    STORE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"sid": old_sid, "slots": {"interest": "视觉算法"}}, ensure_ascii=False),
                 encoding="utf-8")
    t = time.time() - 91 * 86400
    os.utime(p, (t, t))
    got = mem.load(old_sid)
    check("90 天后自动失效", got is None and not p.exists(), f"load={got}")

    # 清理本次产生的文件
    for s in (sid, other, old_sid):
        mem.clear(s)

    print(f'{"场景":20s} {"结果":6s} 说明')
    for name, passed, msg in rows:
        print(f'{name:20s} {"✅" if passed else "❌":6s} {"" if passed else msg}')
    print(f"\n记忆测试：{ok} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
