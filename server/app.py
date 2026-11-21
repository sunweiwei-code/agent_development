# -*- coding: utf-8 -*-
"""
本地服务（标准库 HTTP，不依赖 FastAPI/Flask —— 本机 pip 装包不可用，见 PRD 15.18）。

路由：
  GET  /                 主页（三个入口，不放任何一端的界面）
  GET  /chat             学生端单页应用（身份 sid 在这里下发）
  GET  /teacher          教师端
  GET  /lab[/<id>]       实验室档案页
  GET  /api/overview     首页卡片上的数字
  GET  /api/hotlabs      首页气泡：今天该飘哪 12 个实验室
  GET  /api/stream       SSE 流：逐阶段推送轨迹，最后推 result
  POST /api/chat         一次性返回（备用/便于脚本调用）
  GET  /api/lab/<id>     实验室详情
  GET  /api/memory       学生跨会话记忆（画像摘要）
  POST /api/memory/clear 清除记忆（换一个新身份）

SSE 为什么用 GET：浏览器 EventSource 只支持 GET，所以把 text / history 编码进查询参数。

启动：
    python server/app.py            # 默认 127.0.0.1:8000
    python server/app.py 8080
"""
import importlib
import json
import queue
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline as _pl  # noqa: E402
import teacher as _tc  # noqa: E402
import memory as _mem  # noqa: E402
import hot as _hot  # noqa: E402
recommend = _pl.recommend
from tracing import log_turn_async  # noqa: E402

STATIC = ROOT / "server" / "static"
SID_COOKIE = "lrsid"

# 是否流式生成推荐理由。
# False = 阻塞式：等理由整段生成完，随 result 一次性返回（本轮按需求改回）
# True  = 流式：逐字推送（pipeline.on_token）
# 开发期自动重载：改了 scripts/*.py 不必手动重启（踩过多次"改了没生效"）
_WATCH = [ROOT / "scripts" / x for x in ("llm.py", "score.py", "reasons.py", "pipeline.py")]
_MTIME = {}


def _maybe_reload():
    global recommend
    changed = []
    for f in _WATCH:
        try:
            m = f.stat().st_mtime
        except FileNotFoundError:
            continue
        if _MTIME.get(f) != m:
            changed.append(f)
            _MTIME[f] = m
    if not changed:
        return False
    try:
        for name in ("llm", "score", "reasons", "pipeline"):
            mod = sys.modules.get(name)
            if mod:
                importlib.reload(mod)
        recommend = sys.modules["pipeline"].recommend
        print(f"[reload] 已重载：{[f.name for f in changed]}")
        return True
    except Exception as e:
        print(f"[reload] 失败（继续用旧模块）：{type(e).__name__}: {e}", file=sys.stderr)
        return False


STREAM_REASONS = False

# 快照卡片要写"改了什么"，不能甩 snap-20261120-162856-L04 给老师看
FIELD_CN = {"name": "实验室名称", "intro": "实验室简介", "research_areas": "研究方向",
            "achievements": "代表成果", "group_tags": "组别", "recruit_info": "招新信息",
            "contact": "联系方式", "teacher": "指导老师", "location": "地点",
            "direction_notes": "方向说明", "path_type": "培养类型"}
SUB_CN = {"window": "招新时间", "accepts_freshman": "是否招大一", "note": "补充说明",
          "qq_group": "QQ 群", "email": "邮箱", "wechat": "微信"}
VAL_CN = {"yes": "招大一", "likely": "大概招大一", "senior_only": "只招大二以上",
          "unclear": "未说明"}


def _cn_sub(k):
    return SUB_CN.get(k, k)


def _cn_val(v):
    if v is None or v == "":
        return "未填写"
    return VAL_CN.get(v, str(v))


def _brief(v, n=40):
    if isinstance(v, list):
        s = "、".join(str(x) for x in v)
    elif isinstance(v, dict):
        s = "；".join(f"{_cn_sub(k)}：{_cn_val(x)}" for k, x in v.items() if x not in (None, ""))
    else:
        s = _cn_val(v)
    return s if len(s) <= n else s[:n] + "…"


def _patch_detail(p):
    op, new, old = p.get("op"), p.get("new_value"), p.get("old_value")
    if op == "merge" and isinstance(new, dict):
        return "；".join(f"{_cn_sub(k)} {_cn_val((old or {}).get(k))} → {_cn_val(v)}"
                         for k, v in new.items())
    if op == "append":
        return f"新增 {_brief(new)}"
    if op == "remove":
        return f"删除 {_brief(new)}"
    return f"{_brief(old)} → {_brief(new)}"


def _snap_list():
    """快照 + 审计合流：左栏每张卡片说明这是谁的哪次改动。

    单看目录名只能给出时间戳，老师无法判断该回滚哪一个。
    """
    d = ROOT / "data/teacher/snapshots"
    if not d.exists():
        return []
    names = sorted([x.name for x in d.iterdir() if x.is_dir()], reverse=True)[:30]

    by_snap, lab_name = {}, {}
    af = ROOT / "data/teacher/audit.jsonl"
    if af.exists():
        for line in af.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            sid = r.get("snapshot")
            if not sid:
                continue
            # 编辑记录带 patches，信息量比回滚记录大，优先保留
            if r.get("patches") or sid not in by_snap:
                by_snap[sid] = r
    try:
        for l in json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8")):
            lab_name[l["lab_id"]] = l.get("name") or l["lab_id"]
    except Exception:
        pass

    out = []
    for n in names:
        # snap-20261120-162856-L04
        raw = n[5:] if n.startswith("snap-") else n
        ts, _, lab_id = raw.partition("-L")
        lab_id = ("L" + lab_id) if lab_id else ""
        try:
            when = f"{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
        except Exception:
            when = ts
        rec = by_snap.get(n) or {}
        patches = rec.get("patches") or []
        out.append({
            "id": n,
            "ts": when,
            "lab_id": rec.get("lab_id") or lab_id,
            "lab_name": lab_name.get(rec.get("lab_id") or lab_id, rec.get("lab_id") or lab_id),
            "what": "、".join(FIELD_CN.get(p.get("field"), p.get("field", "")) for p in patches) or "回滚点",
            "detail": "；".join(_patch_detail(p) for p in patches),
            "instruction": rec.get("instruction") or "",
            "kind": "edit" if patches else "rollback",
        })
    return out


def _lab(lab_id):
    """按需读取，避免服务启动后数据改了仍返回旧值（名称修正踩过）。"""
    for l in json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8")):
        if l["lab_id"] == lab_id:
            return l
    return None


def _overview():
    """首页那三张卡上的数字。实时算，不写死——档案改了首页还报旧数会骗人。"""
    labs = json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8"))
    return {
        "labs": len(labs),
        "achievements": sum(len(l.get("achievements") or []) for l in labs),
        "freshman_ok": sum(1 for l in labs
                           if (l.get("recruit_info") or {}).get("accepts_freshman") == "yes"),
    }


def _count_labs(result):
    """这一轮被问到的实验室记一次咨询（首页气泡按它排序）。"""
    try:
        _hot.hit([t.get("lab_id") for t in (result.get("top3") or [])])
    except Exception as e:          # 计数失败不能影响回答
        print(f"[hot] 计数失败：{type(e).__name__}: {e}")


def _fresh_keys(steps):
    """本轮原文里新抽到的槽位（pipeline 的 extract_done 步骤带出来的）。"""
    for s in steps:
        if s.get("stage") == "extract_done" and isinstance(s.get("data"), dict):
            return dict(s["data"])
    return {}


def _recall_note(mem, result, fresh, client_history):
    """这一轮有哪几条是"上次说的"在起作用，没参与判断的不报。

    不算清楚就会撒谎：学生问"介绍一下 L04"时也要弹"接着你上次说的"，
    那是把详情当成了记忆的功劳。
    也不能只看"槽位名有没有出现在本轮抽取里"——学生说"接着上次"时，
    模型常把 goal 抽成"未定"，而合并规则会保留记忆里的"竞赛"，
    按名字算就把这条功劳记错了。所以要比**值**。
    """
    if not mem or result.get("form") in ("追问", None):
        return None
    slots = result.get("slots") or {}
    client = (client_history or {}).get("slots") or {}
    used = {k for k, v in slots.items()
            if k not in client and (fresh or {}).get(k) != v}
    return _mem.profile(mem, used) or None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 只留错误，避免每个请求都刷一行
        if str(args[1] if len(args) > 1 else "") >= "400":
            print(f"[http] {fmt % args}")

    # ---------- 静态 ----------
    def _file(self, name, ctype, cookie=None):
        p = STATIC / name
        if not p.exists():
            self._json({"error": "not found"}, 404)
            return
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 全是实时算的：首页气泡尤其不能吃缓存，否则"每日刷新"要等到明天才生效
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    # ---------- 学生身份 ----------
    def _cookie(self, key):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == key:
                return v.strip()
        return None

    def _sid(self, qs=None):
        """URL 里的 sid（脚本/截图用）> Cookie > 新发一个。

        返回 (sid, 是否新发)。学生端没有登录，身份只是一个随机串，
        它跟姓名学号没有任何映射——见 scripts/memory.py 顶部说明。
        """
        raw = (qs.get("sid") or [None])[0] if qs else None
        if not _mem.valid_sid(raw):
            raw = self._cookie(SID_COOKIE)
        if _mem.valid_sid(raw):
            return raw, False
        return _mem.new_sid(), True

    def _sid_cookie(self, sid):
        return (f"{SID_COOKIE}={sid}; Path=/; Max-Age={_mem.TTL_DAYS * 86400}; "
                f"HttpOnly; SameSite=Lax")

    # ---------- 路由 ----------
    def do_GET(self):
        _maybe_reload()
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        # 主页是门厅，学生端搬到 /chat。
        # 身份（sid）跟学生端走：主页不发 cookie，否则每个路过首页的人都被记一条。
        if u.path in ("/", "/home"):
            return self._file("home.html", "text/html")
        if u.path in ("/chat", "/index.html"):
            sid, fresh = self._sid(qs)
            return self._file("index.html", "text/html",
                              cookie=self._sid_cookie(sid) if fresh else None)
        if u.path == "/api/overview":
            return self._json(_overview())
        if u.path == "/api/hotlabs":
            return self._json(_hot.top())
        if u.path == "/api/memory":
            _mem.maybe_sweep()
            sid, fresh = self._sid(qs)
            mem = _mem.load(sid)
            out = _mem.public(mem)
            out["sid"] = sid
            return self._json(out, cookie=self._sid_cookie(sid) if fresh else None)
        if u.path.startswith("/static/"):
            name = u.path[len("/static/"):]
            if "/" in name or ".." in name:
                return self._json({"error": "bad path"}, 400)
            ext = name.rsplit(".", 1)[-1].lower()
            ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "webp": "image/webp", "html": "text/html",
                     "svg": "image/svg+xml", "css": "text/css", "js": "application/javascript"}.get(ext, "application/octet-stream")
            return self._file(name, ctype)
        if u.path == "/api/stream":
            return self._stream(urllib.parse.parse_qs(u.query))
        if u.path == "/teacher":
            return self._file("teacher.html", "text/html")
        if u.path == "/lab" or u.path.startswith("/lab/"):
            return self._file("lab.html", "text/html")
        if u.path == "/api/teacher/labs":
            labs = json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8"))
            return self._json([{"lab_id": l["lab_id"], "name": l["name"],
                                "path_type": (l.get("path_type") or ["待确认"])[0]} for l in labs])
        if u.path == "/api/teacher/audit":
            f = ROOT / "data/teacher/audit.jsonl"
            rows = []
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
            names = {}
            try:
                for l in json.loads((ROOT / "data/labs/labs.json").read_text(encoding="utf-8")):
                    names[l["lab_id"]] = l.get("name") or l["lab_id"]
            except Exception:
                pass
            for r in rows:
                # 老记录的回滚条目没写 lab_id，从快照名里补，避免显示成"全局"
                if not r.get("lab_id") and (r.get("snapshot") or "").count("-L"):
                    tail = r["snapshot"].rsplit("-L", 1)[-1]
                    r["lab_id"] = "L" + tail
                r["lab_name"] = names.get(r.get("lab_id"), r.get("lab_id") or "全局")
                r["user"] = {"system": "系统", "teacher": "老师"}.get(r.get("user"), r.get("user") or "")
            return self._json(rows[-40:][::-1])
        if u.path == "/api/teacher/snapshots":
            return self._json(_snap_list())
        if u.path.startswith("/api/lab/"):
            lid = u.path.rsplit("/", 1)[-1]
            lab = _lab(lid)
            # 只有学生真的打开了某一份档案才计数（lab.html 带 hit=1）；
            # 档案库列表会一次性拉 45 个详情、老师端也会拉，那些不算"咨询"。
            if lab and (qs.get("hit") or [""])[0] in ("1", "true"):
                _hot.hit([lid])
            return self._json(lab or {"error": "unknown lab"}, 200 if lab else 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        _maybe_reload()
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/api/memory/clear":
            sid, _ = self._sid(qs)
            _mem.clear(sid)
            # 换一个新身份：老的串已经没有任何数据，再沿用只会让"清除"看起来没生效
            new = _mem.new_sid()
            return self._json({"ok": True, "sid": new}, cookie=self._sid_cookie(new))
        if u.path.startswith("/api/teacher/"):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            try:
                if u.path == "/api/teacher/parse":
                    return self._json(_tc.propose(body.get("text", ""), body.get("lab_id")))
                if u.path == "/api/teacher/apply":
                    return self._json(_tc.apply_proposals(body.get("proposals") or [],
                                                          body.get("lab_id"),
                                                          instruction=body.get("text", "")))
                if u.path == "/api/teacher/rollback":
                    return self._json(_tc.rollback(body.get("snapshot", "")))
            except Exception as e:
                return self._json({"ok": False, "code": f"{type(e).__name__}: {e}"}, 500)
            return self._json({"error": "not found"}, 404)

        if u.path != "/api/chat":
            return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        text = (payload.get("text") or "").strip()
        if not text:
            return self._json({"error": "empty text"}, 400)
        # fresh=1：测试与"换个身份"用，既不读也不写记忆
        no_mem = str(payload.get("fresh") or (qs.get("fresh") or [""])[0]) in ("1", "true")
        sid, fresh_sid = self._sid(qs)
        # 没有身份（既没 cookie 也没 sid）就不写记忆：这类请求是脚本直接打接口，
        # 给它凭空造一个身份只会让 data/students/ 每跑一次测试多一个垃圾文件。
        # 真实浏览器一定先过 "/"，那时已经下发 cookie 了。
        track = (not no_mem) and (not fresh_sid)   # 注意：track ≠ mem 是否为 None
        mem = _mem.load(sid) if track else None
        client_hist = payload.get("history")
        hist = None if no_mem else _mem.merge_history(mem, client_hist)
        if hist is None:
            hist = client_hist
        try:
            t0 = __import__("time").time()
            steps = []
            r = recommend(text, history=hist,
                          on_step=lambda stage, msg, data=None: steps.append(
                              {"stage": stage, "msg": msg, "data": data,
                               "ts": __import__("datetime").datetime.now(
                                   __import__("datetime").timezone.utc)}))
            log_turn_async(text, r, steps, round(__import__("time").time() - t0, 2), hist)
            if track:
                _count_labs(r)
                r["recall"] = _recall_note(mem, r, _fresh_keys(steps), client_hist)
                after = _mem.observe(sid, text, r)
                if after:
                    r["memory"] = _mem.public(after)
            return self._json(r, cookie=self._sid_cookie(sid) if fresh_sid else None)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------- SSE ----------
    def _stream(self, qs):
        text = (qs.get("text") or [""])[0].strip()
        hist = qs.get("history") or [None]
        try:
            history = json.loads(hist[0]) if hist[0] else None
        except json.JSONDecodeError:
            history = None

        # 跨会话记忆：这一轮开始前把"上次说到哪"并进 history
        no_mem = (qs.get("fresh") or [""])[0] in ("1", "true")
        sid, fresh_sid = self._sid(qs)
        # 同上：没有身份就不落记忆，避免脚本调用留下一地垃圾文件
        track = (not no_mem) and (not fresh_sid)
        mem = _mem.load(sid) if track else None
        client_hist = history
        if mem:
            history = _mem.merge_history(mem, history)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        if fresh_sid:
            self.send_header("Set-Cookie", self._sid_cookie(sid))
        # HTTP/1.1 下必须声明分块，否则客户端会一直等 Content-Length（实测 curl 无输出）
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        q: "queue.Queue" = queue.Queue()
        steps = []

        def worker():
            import time as _t
            from datetime import datetime, timezone as _tz
            t0 = _t.time()
            def on_step(stage, msg, data=None):
                st = {"stage": stage, "msg": msg, "data": data,
                      "ts": datetime.now(_tz.utc)}
                steps.append(st)
                q.put(("step", {"stage": stage, "msg": msg, "data": data}))

            def on_token(lab_id, delta, reset=False, start=False):
                # 仅流式模式下会走到这里
                q.put(("token", {"lab_id": lab_id, "delta": delta,
                                 "reset": bool(reset), "start": bool(start)}))

            try:
                r = recommend(text, history=history, on_step=on_step,
                              on_token=on_token if STREAM_REASONS else None)
                log_turn_async(text, r, steps, round(_t.time() - t0, 2), history)
                # 写完再回填 memory，前端不用多发一个请求（也避免与下一轮抢时序）
                if track:
                    _count_labs(r)
                    r["recall"] = _recall_note(mem, r, _fresh_keys(steps), client_hist)
                    after = _mem.observe(sid, text, r)
                    if after:
                        r["memory"] = _mem.public(after)
                q.put(("result", r))
            except Exception as e:
                q.put(("error", {"msg": f"{type(e).__name__}: {e}"}))
            finally:
                q.put(("end", None))

        threading.Thread(target=worker, daemon=True).start()

        def send(event, data):
            payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        try:
            while True:
                kind, data = q.get(timeout=120)
                if kind == "end":
                    send("end", {})
                    self.wfile.write(b"0\r\n\r\n")   # 结束分块
                    self.wfile.flush()
                    break
                send(kind, data)
        except Exception:
            pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"服务已启动： http://127.0.0.1:{port}")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
