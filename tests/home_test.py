# -*- coding: utf-8 -*-
"""
首页气泡回归：哪些实验室该飘在 logo 周围、凭什么排这个序、点了去哪。

三件事都容易"看起来对、其实错"，而且都不报错：
  ① 排序：计数接口写歪了，气泡顺序就是随机的，没人看得出；
  ② 补齐：热门不足 12 个时要随机补满，但同一天反复刷新必须是同一组
     （每次刷新换一批，用户会以为页面在抽风）；
  ③ 计数来源：老师打开档案是校对，不是咨询；档案库一次拉 45 个详情更不是。
     这两条要是算进去，首页就会长期被"老师最近改过的那几个"占着。

用法：
    python tests/home_test.py
"""
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
HOT = ROOT / "data" / "labs" / "hot.json"
LABS = ROOT / "data" / "labs" / "labs.json"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, r.read().decode("utf-8")


def api(path):
    return json.loads(get(path)[1])


def labs_index():
    return {l["lab_id"]: l.get("name") for l in json.loads(LABS.read_text(encoding="utf-8"))}


# ---------- 场景 ----------

def t_shape(d):
    """12 个、名次连续、id 不重复、名字都有、都真在实验室表里。"""
    labs = api("/api/hotlabs").get("labs") or []
    idx = labs_index()
    ranks = [x.get("rank") for x in labs]
    ids = [x.get("lab_id") for x in labs]
    ok = (len(labs) == 12
          and ranks == list(range(1, 13))
          and len(set(ids)) == 12
          and all(x.get("name") for x in labs)
          and all(i in idx for i in ids))
    return ok, f"{len(labs)} 个 / 名次 {ranks[:3]}… / 唯一 {len(set(ids))} / 都在表里 {all(i in idx for i in ids)}"


def t_stable(d):
    """同一天两次请求必须是同一组：刷新页面不该换一批实验室。"""
    a, b = api("/api/hotlabs"), api("/api/hotlabs")
    ok = [x["lab_id"] for x in a["labs"]] == [x["lab_id"] for x in b["labs"]]
    return ok, "两次一致" if ok else "两次不一致"


def t_count_ranks(d):
    """计数高的必须排前面，且标成 hot。"""
    api("/api/hotlabs")            # 热身，确保服务已起
    c = json.loads(HOT.read_text(encoding="utf-8")) if HOT.exists() else {"counts": {}}
    c["counts"] = {"L45": 9, "L02": 4}
    HOT.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    try:
        d = api("/api/hotlabs")
        top = [x["lab_id"] for x in d["labs"][:2]]
        src = [x["source"] for x in d["labs"][:2]]
        ok = top == ["L45", "L02"] and src == ["hot", "hot"] and d.get("hot") == 2
        return ok, f"前两名={top} 来源={src} 热门数={d.get('hot')}"
    finally:
        pass                            # 由 main 里的 finally 统一还原


def t_fill_random(d):
    """热门不足 12 个时，剩下的由随机位补齐，且名次仍然连续。"""
    d = api("/api/hotlabs")
    rand = [x for x in d["labs"] if x["source"] == "random"]
    ok = (len(d["labs"]) == 12 and len(rand) == 12 - d.get("hot", 0)
          and all(x["count"] == 0 for x in rand))
    return ok, f"随机补 {len(rand)} 个 / 热门 {d.get('hot')} 个"


def t_hit_only_explicit(d):
    """只有带 hit=1 才计数：档案库/教师端那种批量拉详情不能算咨询。"""
    before = (api("/api/hotlabs"), json.loads(HOT.read_text(encoding="utf-8"))["counts"])
    get("/api/lab/L45")                       # 不带 hit：不计数
    mid = json.loads(HOT.read_text(encoding="utf-8"))["counts"]
    get("/api/lab/L45?hit=1")                 # 带 hit：计一次
    after = json.loads(HOT.read_text(encoding="utf-8"))["counts"]
    ok = mid.get("L45", 0) == before[1].get("L45", 0) and after.get("L45", 0) == mid.get("L45", 0) + 1
    return ok, f"不带 hit 不变={mid.get('L45', 0)}，带 hit +1={after.get('L45', 0)}"


def t_lab_page_gate(d):
    """档案页只在学生侧带 hit=1；从教师端进来是校对，不该算咨询。"""
    src = (ROOT / "server" / "static" / "lab.html").read_text(encoding="utf-8")
    ok = "?hit=1" in src and "FROM === 'teacher'" in src
    return ok, "lab.html 里 hit 受 from 控制" if ok else "lab.html 的 hit 逻辑不见了"


def t_home_wiring(d):
    """首页要真的接上：接口、眨眼图、回主页的来源参数、气泡容器。"""
    src = (ROOT / "server" / "static" / "home.html").read_text(encoding="utf-8")
    need = ["/api/hotlabs", "agent-wink.jpg", "from=home", 'id="bubbles"',
            "const N = 12"]
    miss = [k for k in need if k not in src]
    return not miss, ("都接上了" if not miss else f"缺 {miss}")


SCEN = [
    ("接口形状", t_shape),
    ("同日稳定", t_stable),
    ("计数排序", t_count_ranks),
    ("随机补齐", t_fill_random),
    ("只认显式计数", t_hit_only_explicit),
    ("档案页来源门", t_lab_page_gate),
    ("首页接线", t_home_wiring),
]


def main():
    backup = HOT.read_bytes() if HOT.exists() else None
    print(f"\n{'场景':<16}{'结果':<6}说明")
    print("-" * 76)
    bad = 0
    try:
        for name, fn in SCEN:
            try:
                ok, why = fn(None)
            except Exception as e:
                ok, why = False, f"异常 {type(e).__name__}: {e}"
            bad += (not ok)
            print(f"{name:<16}{'✅' if ok else '❌':<6}{why}")
    finally:
        if backup is None:
            HOT.unlink(missing_ok=True)
        else:
            HOT.write_bytes(backup)

    total = len(SCEN)
    print(f"\n首页气泡测试：{total - bad} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
