# -*- coding: utf-8 -*-
"""
UI 端到端测试：无头 Edge 打开页面 → 自动回放对话 → 截图 + 抓 DOM → 断言。

为什么这样做：本机没有 Playwright/Selenium（pip 拉不动），但 Edge 自带
`--headless --dump-dom --screenshot` 两个能力，配合页面里的 `?auto=` 回放参数，
就能做到"真实点击 → 看结果"的自动化，而不是只检查 CSS 字符串。

用法：
    python tests/ui_test.py            # 全场景
    python tests/ui_test.py --only cmp # 只跑对比
"""
import html as html_lib
import json
import re
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = ROOT / "tests" / "edge-profile"
SHOTS = ROOT / "shots"
ROOT_URL = "http://127.0.0.1:8000/"          # 主页与接口
BASE = ROOT_URL + "chat"                       # 学生端（V3.57 起从 / 搬到 /chat）
BUDGET = "30000"

SCEN = [
    # name, 对话序列, 断言函数
    ("rec-3", ["我想做视觉算法，想打比赛"],
     lambda d: (d.count('class="card"') == 3, f'卡片 {d.count(chr(34) + "card" + chr(34))} 张（期望 3）')),

    ("detail", ["我想做视觉算法，想打比赛", "详细介绍一下"],
     lambda d: (("实验室详情" in d or "的详细情况" in d) and "代表成果" in d,
                "详情页需含「详细情况」与「代表成果」")),

    ("cmp-2", ["我想做视觉算法，想打比赛", "对比一下前两个"],
     lambda d: ('class="cmp' in d and d.count("<col>") == 2, f'对比表列数 {d.count("<col>")-1}（期望 2）')),

    ("cmp-2b", ["我想做视觉算法，想打比赛", "对比一下后面这两个"],
     lambda d: ('class="cmp' in d and d.count("<col>") == 2, '后面这两个应为 2 列')),

    ("cmp-3", ["我想做视觉算法，想打比赛", "对比一下这三个"],
     lambda d: ('class="cmp' in d and d.count("<col>") == 3, f'三室对比列数 {d.count("<col>")-1}（期望 3）')),

    ("no-hardcrimp", ["我想做视觉算法，想打比赛"],
     lambda d: ("没有硬凑" not in d and "Top5" not in d, "不应出现「没有硬凑」与 Top5 提示")),

    ("no-double-bubble", ["我想做视觉算法，想打比赛"],
     lambda d: (d.count("还想进一步了解什么呢") == 1,
                f'收尾句出现 {d.count("还想进一步了解什么呢")} 次（期望 1）')),
]


def run(url, dump=True, shot=None):
    args = [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={PROFILE}",
            "--window-size=1440,900", f"--virtual-time-budget={BUDGET}"]
    if dump:
        args.append("--dump-dom")
    if shot:
        args.append(f"--screenshot={shot}")
    args.append(url)
    r = subprocess.run(args, capture_output=True, timeout=180)
    return r.stdout.decode("utf-8", "ignore")


def mem_case():
    """回来的人：开场白要交代"我记得什么"，回答要标出"接着上次说的"。

    这一场不能加 fresh=1——它测的就是记忆链路的**渲染**，所以先种一轮再打开。
    """
    sid = "s-ui" + secrets.token_hex(4)
    body = json.dumps({"text": "我想做视觉算法，想打比赛，大一"}).encode()
    req = urllib.request.Request(f"{ROOT_URL}api/chat?sid={sid}", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        seed = json.loads(r.read().decode("utf-8"))

    auto = urllib.parse.quote(json.dumps(["接着上次的继续推荐"], ensure_ascii=False))
    url = f"{BASE}?sid={sid}&auto={auto}"
    raw = run(url, dump=True)
    dom = re.sub(r"<script[\s\S]*?</script>", "", raw)
    run(url, dump=False, shot=str(SHOTS / "mem-back.png"))
    urllib.request.urlopen(urllib.request.Request(
        f"{ROOT_URL}api/memory/clear?sid={sid}", data=b"{}", method="POST"), timeout=30).read()

    if 'data-auto="done"' not in raw:
        return False, "页面未跑完"
    if seed.get("form") != "①":
        return False, f"种子轮次形态 {seed.get('form')}"
    if "又见面了" not in dom or "id=\"mem-txt\"" not in dom or "视觉算法" not in dom:
        return False, "开场白没交代记得什么"
    if 'class="recall"' not in dom or "接着你上次说的" not in dom:
        return False, "气泡顶部缺「接着上次说的」说明"
    if dom.count('class="card"') < 2:
        return False, f'卡片 {dom.count(chr(34) + "card" + chr(34))} 张'
    return True, ""


# 预选框点击的探针页：套一层同源 iframe，因为 restoreChat 读的是 sessionStorage，
# 命令行没法先写进目标标签页。iframe 里加载 /chat，把结果抄到外层 body 的 data-report。
TAP = """<!DOCTYPE html><meta charset="utf-8">
<style>html,body{margin:0}iframe{border:0;width:1200px;height:820px}</style>
<script>
const mode=new URLSearchParams(location.search).get('m')||'fresh';
if(mode==='restore'){
  /* 模拟「刷新页面 / 从档案页点回到对话」：先写好上一段的 HTML，再打开 /chat，
     走的就是 restoreChat 那条路——那里的按钮是 chat.innerHTML 贴回来的。 */
  const fake='<div class="row"><img class="ava" src="/static/avatar.png"><div class="bub">'
    +'<div class="q">\\u4f60\\u597d\\uff0c\\u6211\\u662f\\u5b9e\\u9a8c\\u5ba4\\u63a8\\u8350\\u52a9\\u624b\\u3002</div>'
    +'<div class="samples"><button data-t="\\u968f\\u4fbf\\u770b\\u770b">\\u968f\\u4fbf\\u770b\\u770b</button>'
    +'<button data-t="\\u6211\\u60f3\\u505a\\u89c6\\u89c9\\u7b97\\u6cd5\\uff0c\\u60f3\\u6253 RoboMaster\\uff0c\\u5927\\u4e00">'
    +'\\u89c6\\u89c9 + \\u6bd4\\u8d5b</button></div></div></div>';
  sessionStorage.setItem('lr-chat',JSON.stringify({h:null,items:[],html:fake}));
}
document.write('<iframe id="f" src="/chat?fresh='+(mode==='fresh'?1:0)+'&v='+Date.now()+'"></iframe>');
addEventListener('load',()=>{
  const f=document.getElementById('f'),w=f.contentWindow,d=f.contentDocument,out={mode,errors:[]};
  w.addEventListener('error',e=>out.errors.push(String(e.message)));
  setTimeout(()=>{
    const b=d.querySelectorAll('.samples button[data-t]');
    out.buttons=b.length;
    if(!b.length){report(out);return;}
    b[b.length-1].click();
    setTimeout(()=>{
      out.sent=[...d.querySelectorAll('.row.me .bub')].map(x=>x.textContent.trim());
      report(out);
    },1800);
  },1600);
});
function report(o){document.body.dataset.report=JSON.stringify(o);}
</script>
"""


def chip_case():
    """预选框点了要有反应——包括「刷新 / 从档案页回来」之后恢复出来的那一份。

    这一条是补 bug 的：restoreChat 用 chat.innerHTML 把旧对话贴回来，innerHTML
    只还原标签、不还原 onclick，所以一段时间的真实表现是「预选框还在，但点不动」。
    现在点击统一交给 chat 上的事件委托，这里从两条路各点一次。
    """
    harness = ROOT / "server" / "static" / ".tap.html"
    harness.write_text(TAP, encoding="utf-8")
    try:
        for mode, want in (("fresh", "随便看看"),
                           ("restore", "我想做视觉算法，想打 RoboMaster，大一")):
            raw = run(f"{ROOT_URL}static/.tap.html?m={mode}&v={secrets.token_hex(3)}", dump=True)
            m = re.search(r'data-report="([^"]+)"', raw)
            if not m:
                return False, f"探针没跑完（{mode}，{len(raw)} 字符）"
            d = json.loads(html_lib.unescape(m.group(1)))
            if d.get("errors"):
                return False, f'{mode} 报错：{d["errors"]}'
            if not d.get("buttons"):
                return False, f"{mode} 页面上没有预选框"
            if d.get("sent") != [want]:
                return False, f'{mode} 点「{want}」后发出的消息是 {d.get("sent")}（应恰好一条）'
        return True, ""
    finally:
        harness.unlink(missing_ok=True)


def main():
    SHOTS.mkdir(exist_ok=True)
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None

    ok = bad = 0
    print(f'{"场景":16s} {"结果":6s} 说明')
    for name, seq, check in SCEN:
        if only and only not in name:
            continue
        auto = urllib.parse.quote(json.dumps(seq, ensure_ascii=False))
        # fresh=1：不读也不写跨会话记忆。否则同一个无头浏览器 profile 跑第二次时
        # 会带着上一场的"视觉算法"，用例就不再是独立可复现的了。
        url = f"{BASE}?auto={auto}&fresh=1"

        raw = run(url, dump=True)
        # 断言必须只看渲染结果：<script> 里的模板字符串也含 class="card" 等字样
        dom = re.sub(r"<script[\s\S]*?</script>", "", raw)
        if 'data-auto="done"' not in raw:
            print(f"{name:16s} ⏱超时 页面未跑完（{len(raw)} 字符）")
            bad += 1
            continue
        passed, msg = check(dom)
        run(url, dump=False, shot=str(SHOTS / f"{name}.png"))
        print(f'{name:16s} {"✅" if passed else "❌":6s} {msg if not passed else ""}'
              f'{"  → shots/" + name + ".png" if passed else ""}')
        ok += passed
        bad += (not passed)

    if not only or "chip" in only:
        passed, msg = chip_case()
        print(f'{"chip-click":16s} {"✅" if passed else "❌":6s} {msg if not passed else ""}')
        ok += passed
        bad += (not passed)

    if not only or "mem" in only:
        passed, msg = mem_case()
        print(f'{"mem-recall":16s} {"✅" if passed else "❌":6s} {msg if not passed else ""}'
              f'{"  → shots/mem-back.png" if passed else ""}')
        ok += passed
        bad += (not passed)

    print(f"\nUI 测试：{ok} 通过 / {bad} 失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
