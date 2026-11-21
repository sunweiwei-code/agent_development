# -*- coding: utf-8 -*-
"""
M0 步骤 ⑤：切块 + 双写（先做结构化落盘 + SQLite FTS5；向量库待装 chromadb 后补）。

对应 PRD：
- 5.1 ⑤ 双写：结构化 JSON + 文本块（带 metadata）→ Chroma；文本块 metadata 含
  lab_id / field / page_idx / bbox
- 8.5 BM25 用 SQLite FTS5；中文用 trigram 分词（无需 jieba）
- V2.2 决策：direction_notes **单独成块**，否则长说明会污染 research_areas 的检索

切块策略：
  intro           按句切，单块 ≤400 字
  research_areas  合并为一块（短标签用「、」连接）
  achievements    每条一块；超长再切
  direction_notes 单独成块，≤400 字
  group_tags      合并为一块（本期仅 L01 有）
  contact         **不进检索**——联系方式属个人信息，且是精确查询而非语义匹配

产出：
  data/chunks/chunks.jsonl
  data/indexes/labs_index.sqlite3（FTS5 + 原生表；向量表由 build_vectors.py 追加）
"""
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABS = ROOT / "data" / "labs" / "labs.json"
CONTENT = ROOT / "data" / "parsed" / "content_list.json"
CHUNKS = ROOT / "data" / "chunks" / "chunks.jsonl"
DB = ROOT / "data" / "indexes" / "labs_index.sqlite3"

MAX_CHARS = 400
SENT_SPLIT = re.compile(r"(?<=[。；！？])")

SKIP_FIELDS = {"contact"}          # 不进检索

CJK_RE = re.compile(r"([\u4e00-\u9fff])")


def space_cjk(t: str) -> str:
    """把每个汉字前后加空格，让 FTS5 的 unicode61 按「字」建索引。

    为什么不用 trigram：trigram 只索引长度 ≥3 的片段，
    **两字查询（视觉/算法/网络/鸿蒙）全部 0 命中**——而中文里两字词最常见。
    按字切分后，"视觉算法" 变成 " 视 觉 算 法 "，可用短语查询精确匹配连续字。
    """
    return CJK_RE.sub(r" \1 ", t or "")


def split_text(t: str, max_len: int = MAX_CHARS):
    """按句切分并按长度聚合，避免把一句话截断。"""
    t = (t or "").strip()
    if not t:
        return []
    if len(t) <= max_len:
        return [t]
    out, buf = [], ""
    for sent in SENT_SPLIT.split(t):
        if not sent:
            continue
        if len(buf) + len(sent) > max_len and buf:
            out.append(buf)
            buf = sent
        else:
            buf += sent
    if buf:
        out.append(buf)
    return out


def load_bbox_index():
    """为每个 (page, 文本前缀) 建 bbox 索引，用于给 chunk 回填 bbox。"""
    raw = json.loads(CONTENT.read_text(encoding="utf-8"))
    idx = {}
    for b in raw:
        t = re.sub(r"^[-\\\s]+", "", (b.get("text") or "").strip())
        if not t or not b.get("bbox"):
            continue
        p = int(b.get("page_idx", 0)) + 1
        for k in (12, 10, 8):
            idx.setdefault((p, t[:k]), b["bbox"])
    return idx


def find_bbox(box_idx, pages, text):
    for k in (12, 10, 8):
        for p in pages:
            hit = box_idx.get((p, text[:k]))
            if hit:
                return p, hit
    for p in pages:
        for (pp, prefix), box in box_idx.items():
            if pp == p and prefix and prefix in text[:24]:
                return p, box
    return (pages[0] if pages else None), None


def build():
    labs = json.loads(LABS.read_text(encoding="utf-8"))
    box_idx = load_bbox_index()

    chunks = []
    for lab in labs:
        pages = lab.get("source_pages") or []
        fields = []

        if lab.get("research_areas"):
            fields.append(("research_areas", "、".join(lab["research_areas"])))
        if lab.get("group_tags"):
            fields.append(("group_tags", "、".join(lab["group_tags"])))
        if lab.get("direction_notes"):
            fields.append(("direction_notes", lab["direction_notes"]))
        if lab.get("intro"):
            fields.append(("intro", lab["intro"]))
        for a in lab.get("achievements") or []:
            fields.append(("achievements", a))

        # achievements 是"每条一个 field 项"，不能对每个条目都从 00 编号，
        # 否则 chunk_id 会重复（UNIQUE 冲突）。按 (lab, field) 统一计数。
        seq = {}
        for field, text in fields:
            if field in SKIP_FIELDS:
                continue
            for piece in split_text(text):
                n = seq.get(field, 0)
                seq[field] = n + 1
                page, box = find_bbox(box_idx, pages, piece)
                chunks.append({
                    "chunk_id": f'{lab["lab_id"]}:{field}:{n:02d}',
                    "lab_id": lab["lab_id"],
                    "lab_name": lab["name"],
                    "field": field,
                    "text": piece,
                    "page_idx": page,
                    "bbox": box,
                    "duplicate_of": lab.get("duplicate_of"),
                    "field_source": lab["field_sources"].get(field),
                })

    CHUNKS.parent.mkdir(parents=True, exist_ok=True)
    with CHUNKS.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # ---- FTS5 索引 ----
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""CREATE TABLE chunk_meta(
        chunk_id TEXT PRIMARY KEY, lab_id TEXT, lab_name TEXT, field TEXT,
        text TEXT, page_idx INTEGER, bbox TEXT, duplicate_of TEXT, field_source TEXT)""")
    # unicode61 + 预先按字切分：两字词也能命中（trigram 不行）
    cur.execute("CREATE VIRTUAL TABLE chunk_fts USING fts5(text, chunk_id UNINDEXED, tokenize='unicode61')")
    tok = "unicode61 + 按字切分"
    for c in chunks:
        cur.execute("INSERT INTO chunk_meta VALUES (?,?,?,?,?,?,?,?,?)",
                    (c["chunk_id"], c["lab_id"], c["lab_name"], c["field"], c["text"],
                     c["page_idx"], json.dumps(c["bbox"]), c["duplicate_of"], c["field_source"]))
        cur.execute("INSERT INTO chunk_fts(text, chunk_id) VALUES (?,?)",
                    (space_cjk(c["text"]), c["chunk_id"]))
    con.commit()

    print(f"chunks: {len(chunks)}  |  分词器: {tok}")
    print("按字段:", dict(Counter(c["field"] for c in chunks)))
    print("覆盖实验室:", len({c["lab_id"] for c in chunks}), "/", len(labs))
    nb = sum(1 for c in chunks if not c["bbox"])
    print(f"bbox 回填: {len(chunks)-nb}/{len(chunks)}（缺 {nb}）")

    # ---- 冒烟测试 ----
    print("\n=== 检索冒烟 ===")
    for q in ["视觉算法", "算法竞赛", "鸿蒙", "污水", "大语言模型", "量子计算"]:
        # 用 OR 召回：BM25 负责"把可能相关的捞出来"，模糊匹配交给 8.1.1 的方向算子
        # （库里写作"视觉识别算法"，用户问"视觉算法"，短语查询会 0 命中）
        qt = space_cjk(q).strip()
        cur.execute("SELECT chunk_id, lab_name, field, page_idx FROM chunk_fts "
                    "JOIN chunk_meta USING(chunk_id) WHERE chunk_fts MATCH ? ORDER BY rank LIMIT 3", (qt,))
        rows = cur.fetchall()
        print(f'  「{q}」→ {[(r[1][:14], r[2], f"p{r[3]}") for r in rows] if rows else "0 命中"}')
    con.close()


if __name__ == "__main__":
    build()
