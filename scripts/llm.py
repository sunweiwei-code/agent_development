# -*- coding: utf-8 -*-
"""
大模型客户端（PRD 8.5：OpenAI 兼容接口）。

选型：DeepSeek `deepseek-chat`。理由：
  · 中文理解与结构化输出稳定，价格低（本项目一次会话约 3–5 次调用）
  · OpenAI 兼容，换模型只改 .env 里的 BASE_URL / MODEL
  · 与旧项目同一套 key，A/B 可比

不引入 openai SDK：本机 pip 装包不稳定（详见 PRD 15.18），用标准库 urllib 即可。

用法：
    from llm import chat, chat_json
    print(chat([{"role":"user","content":"你好"}]))
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env():
    """读项目 .env（不覆盖已存在的环境变量）。"""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


class LLMError(RuntimeError):
    pass


def chat(messages, model=None, temperature=0.0, max_tokens=1200,
         json_mode=False, timeout=90, retries=2):
    """单轮/多轮对话。json_mode=True 时要求模型返回合法 JSON 对象。"""
    if not API_KEY:
        raise LLMError("未配置 DEEPSEEK_API_KEY（检查项目根目录 .env）")

    body = {
        "model": model or MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            last = LLMError(f"HTTP {e.code}: {msg}")
            if e.code in (429, 500, 502, 503):      # 限流/服务端错误才重试
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last
        except Exception as e:                       # 网络抖动
            last = LLMError(f"{type(e).__name__}: {e}")
            time.sleep(1.5 * (attempt + 1))
    raise last


def chat_stream(messages, model=None, temperature=0.0, max_tokens=1200, timeout=90):
    """流式对话：逐段 yield 文本增量（DeepSeek 的 SSE）。

    为什么要流式：推荐理由原本要等整段生成完再一次性返回，学生盯着空白 2–3 秒。
    改成边生成边显示，首字延迟通常 <1s。
    """
    if not API_KEY:
        raise LLMError("未配置 DEEPSEEK_API_KEY")

    body = {"model": model or MODEL, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens, "stream": True}
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                delta = obj["choices"][0].get("delta", {}).get("content")
            except Exception:
                continue
            if delta:
                yield delta


def chat_json(messages, **kw):
    """要求返回 JSON；解析失败时抛错并带上原文，便于发现"模型没照格式来"。"""
    raw = chat(messages, json_mode=True, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型未返回合法 JSON: {e}\n原文前 300 字: {raw[:300]}")


if __name__ == "__main__":
    t = time.time()
    print(chat([{"role": "user", "content": "用一句话说明什么是实验室推荐系统的冷启动问题"}]))
    print(f"\n耗时 {time.time()-t:.2f}s，模型 {MODEL}")
    print("JSON 模式自测:", chat_json([{"role": "user",
          "content": '返回 JSON：{"ok": true, "note": "测试"}'}]))
