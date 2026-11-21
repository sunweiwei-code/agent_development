# -*- coding: utf-8 -*-
"""
M0 步骤 ⑤（续）：向量嵌入 + 混合检索（BM25 + 向量，RRF 融合）。

为什么不用 chromadb（V2.8 决策，理由写在 PRD 15.18）：
  1. 本机两个 Python 环境都没有 chromadb，`pip install` 连试三次失败（默认源超时、
     清华镜像返回 0 个版本、官方源超时）。
  2. 更关键：**装 chroma 只解决"存向量"，不解决"算向量"**。chroma 默认自带
     ONNXMiniLM_L6_V2（英文 all-MiniLM），拿它算中文实验室描述质量明显更差；
     旧项目当时也没用它，而是显式指定 text-embedding-v4。
  3. 326 块的规模下 HNSW 是过度设计，暴力 cosine 单次 20~50ms，反而更快更可解释。

因此：向量存 SQLite BLOB，cosine 用标准库 array + math 计算；embedding 走
DashScope text-embedding-v4（与旧项目同模型，A/B 可比）。接口写成可替换的，
日后要扩到几千条再换 Chroma/faiss 只改 VectorStore 一个类。

用法：
    python scripts/build_vectors.py            # 建向量 + 冒烟对比
    python scripts/build_vectors.py --search Q  # 单次查询
"""
import array
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "indexes" / "labs_index.sqlite3"
CHUNKS = ROOT / "data" / "chunks" / "chunks.jsonl"
ENV_OLD = Path(r"D:\基于学院实验室介绍的agent（RAG之间的对比）\.env")

MODEL = "text-embedding-v4"
DIM = 1024
BATCH = 10
RRF_K = 60


def load_key():
    """优先环境变量，其次读旧项目 .env（不落盘、不打印）。"""
    k = os.environ.get("DASHSCOPE_API_KEY")
    if k:
        return k
    if ENV_OLD.exists():
        for line in ENV_OLD.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("DASHSCOPE_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 DASHSCOPE_API_KEY")


def embed(texts, key, text_type="document"):
    url = ("https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
           "text-embedding/text-embedding")
    body = {"model": MODEL, "input": {"texts": texts},
            "parameters": {"dimension": DIM, "text_type": text_type}}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        d = json.loads(r.read())
    # 按 index 排序，保证与输入顺序一致
    items = sorted(d["output"]["embeddings"], key=lambda x: x["text_index"])
    return [it["embedding"] for it in items]


def pack(vec):
    return array.array("f", vec).tobytes()


def unpack(b):
    a = array.array("f")
    a.frombytes(b)
    return a


def build():
    key = load_key()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS chunk_vec(
        chunk_id TEXT PRIMARY KEY, dim INTEGER, model TEXT, vec BLOB)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS index_meta(
        k TEXT PRIMARY KEY, v TEXT)""")

    rows = list(cur.execute("SELECT chunk_id, text FROM chunk_meta ORDER BY chunk_id"))
    done = {r[0] for r in cur.execute("SELECT chunk_id FROM chunk_vec")}
    todo = [r for r in rows if r[0] not in done]
    print(f"待嵌入 {len(todo)} / {len(rows)} 块（模型 {MODEL}，维度 {DIM}）")

    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        vecs = embed([b[1] for b in batch], key)
        cur.executemany("INSERT OR REPLACE INTO chunk_vec VALUES (?,?,?,?)",
                        [(b[0], DIM, MODEL, pack(v)) for b, v in zip(batch, vecs)])
        con.commit()
        print(f"  {min(i+BATCH, len(todo))}/{len(todo)} … {time.time()-t0:.1f}s")

    cur.execute("INSERT OR REPLACE INTO index_meta VALUES ('embedding_model',?)", (MODEL,))
    cur.execute("INSERT OR REPLACE INTO index_meta VALUES ('dim',?)", (str(DIM),))
    cur.execute("INSERT OR REPLACE INTO index_meta VALUES ('built_at',?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    con.commit()
    n = cur.execute("SELECT COUNT(*) FROM chunk_vec").fetchone()[0]
    print(f"向量总数 {n}，用时 {time.time()-t0:.1f}s")
    con.close()


class VectorStore:
    """内存态向量检索：启动时把 326 × 1024 读进内存（约 1.3MB），查询走暴力 cosine。"""

    def __init__(self, db=DB):
        con = sqlite3.connect(db)
        self.meta = {k: v for k, v in con.execute("SELECT chunk_id, lab_id||'|'||field FROM chunk_meta")}
        self.vecs = {cid: unpack(b) for cid, b in con.execute("SELECT chunk_id, vec FROM chunk_vec")}
        con.close()

    @staticmethod
    def _cos(a, b):
        s = na = nb = 0.0
        for x, y in zip(a, b):
            s += x * y
            na += x * x
            nb += y * y
        return s / (math.sqrt(na) * math.sqrt(nb) + 1e-9)

    def search(self, qvec, top_k=10, field=None):
        out = []
        for cid, v in self.vecs.items():
            if field and not self.meta[cid].endswith("|" + field):
                continue
            out.append((cid, self._cos(qvec, v)))
        out.sort(key=lambda x: -x[1])
        return out[:top_k]


def bm25_search(con, q, top_k=10, field=None):
    from build_chunks import space_cjk
    qt = space_cjk(q).strip()
    sql = ("SELECT f.chunk_id, rank FROM chunk_fts f JOIN chunk_meta m USING(chunk_id) "
           "WHERE chunk_fts MATCH ?")
    args = [qt]
    if field:
        sql += " AND m.field = ?"
        args.append(field)
    sql += " ORDER BY rank LIMIT ?"
    args.append(top_k)
    return [(r[0], r[1]) for r in con.execute(sql, args)]


def rrf(rank_lists, k=RRF_K):
    """RRF 融合：score = Σ 1/(k + rank)。不需要两路分数量纲对齐。"""
    score = {}
    for lst in rank_lists:
        for i, (cid, _) in enumerate(lst, start=1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + i)
    return sorted(score.items(), key=lambda x: -x[1])


def smoke():
    key = load_key()
    con = sqlite3.connect(DB)
    vs = VectorStore()
    print("\n=== 三路对比（query → top3 实验室/字段）===")
    for q in ["视觉算法", "算法竞赛", "鸿蒙", "污水", "大语言模型", "量子计算"]:
        qv = embed([q], key, text_type="query")[0]
        vec = vs.search(qv, 3)
        bm = bm25_search(con, q, 3)
        hy = rrf([bm, vec])[:3]
        def fmt(rs):
            if not rs:
                return "—"
            return " / ".join(f'{vs.meta[c].split("|")[0]}({vs.meta[c].split("|")[1][:4]},{s:.3f})'
                              if isinstance(s, float) and abs(s) <= 1 else
                              f'{vs.meta[c].split("|")[0]}({vs.meta[c].split("|")[1][:4]})'
                              for c, s in rs)
        print(f'「{q}」\n  BM25 : {fmt(bm)}\n  向量 : {fmt(vec)}\n  混合 : {fmt(hy)}')
    con.close()


if __name__ == "__main__":
    if "--search" in sys.argv:
        q = sys.argv[sys.argv.index("--search") + 1]
        key = load_key()
        con = sqlite3.connect(DB)
        vs = VectorStore()
        qv = embed([q], key, text_type="query")[0]
        vec, bm = vs.search(qv, 5), bm25_search(con, q, 5)
        print("BM25:", [(c, round(s, 3)) for c, s in bm])
        print("向量:", [(c, round(s, 3)) for c, s in vec])
        print("混合:", [c for c, _ in rrf([bm, vec])[:5]])
    else:
        build()
        smoke()
