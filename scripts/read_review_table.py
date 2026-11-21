# -*- coding: utf-8 -*-
"""
读取复核表 —— 同时支持 CSV（UTF-8-BOM）与「Excel 另存但保留 .csv 扩展名」的 xlsx。

背景：复核人用 Excel 打开再保存后，文件实际变成了 xlsx（ZIP 包，开头 PK\\x03\\x04），
但扩展名仍是 .csv。`csv.DictReader` 会直接 UnicodeDecodeError。
本模块用标准库 zipfile + ElementTree 解析，不依赖 openpyxl。

对外只暴露 read_table(path) -> (header list, rows list[dict])
"""
import csv
import io
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    """A1 → 0, B1 → 1, AA1 → 26"""
    s = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _read_xlsx(path: Path):
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    grid = []
    for row in sheet.iter(f"{NS}row"):
        cells = {}
        maxc = -1
        for c in row.findall(f"{NS}c"):
            idx = _col_index(c.get("r", "A1"))
            t = c.get("t")
            v = c.find(f"{NS}v")
            if t == "inlineStr":
                isn = c.find(f"{NS}is")
                val = "".join(x.text or "" for x in isn.iter(f"{NS}t")) if isn is not None else ""
            elif v is None:
                val = ""
            elif t == "s":
                val = shared[int(v.text)]
            else:
                val = v.text or ""
            cells[idx] = val
            maxc = max(maxc, idx)
        grid.append([cells.get(i, "") for i in range(maxc + 1)])
    if not grid:
        return [], []
    header = [h.strip() for h in grid[0]]
    rows = []
    for r in grid[1:]:
        r = r + [""] * (len(header) - len(r))
        rows.append({header[i]: r[i] for i in range(len(header))})
    return header, rows


def read_table(path):
    p = Path(path)
    head = p.read_bytes()[:4]
    if head[:2] == b"PK":                       # 实际是 xlsx
        return _read_xlsx(p)
    # 尝试 UTF-8-BOM，失败再退 GB18030（Excel 另存 CSV 的常见编码）
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = p.read_text(encoding=enc)
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            return list(reader.fieldnames or []), rows
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"{p.name}: 无法识别编码，请另存为 UTF-8 CSV 或 xlsx")


if __name__ == "__main__":
    h, r = read_table(Path(__file__).resolve().parents[1] / "data/review/path_type_review.csv")
    print("列:", h)
    print("行数:", len(r))
    print("样例:", {k: r[0][k] for k in list(r[0])[:6]})
