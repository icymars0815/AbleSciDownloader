"""Memory-only CSV/DOCX/Word97+ DOC extraction. Never executes document macros."""
import csv
import io
import re
import struct
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
import olefile
from paste_parser import make_record, parse_references, split_authors


def doc_text(binary):
    with olefile.OleFileIO(io.BytesIO(binary)) as ole:
        word = ole.openstream("WordDocument").read()
        if len(word) < 160 or struct.unpack_from("<H", word)[0] != 0xA5EC:
            raise ValueError("此 DOC 不是支持的 Word 97+ 文档，请另存为 DOCX")
        flags = struct.unpack_from("<H", word, 10)[0]
        if flags & (0x100 | 0x8000):
            raise ValueError("不读取加密 DOC，请在本机解锁后另存 DOCX")
        table = ole.openstream("1Table" if flags & 0x200 else "0Table").read()
        csw = struct.unpack_from("<H", word, 32)[0]
        cslw_offset = 34 + csw * 2
        cslw = struct.unpack_from("<H", word, cslw_offset)[0]
        ccp = struct.unpack_from("<I", word, cslw_offset + 2 + 12)[0]
        rg = cslw_offset + 2 + cslw * 4 + 2
        fc, length = struct.unpack_from("<II", word, rg + 33 * 8)
        clx = table[fc:fc + length]
        pos = 0
        while pos < len(clx) and clx[pos] == 1:
            pos += 3 + struct.unpack_from("<H", clx, pos + 1)[0]
        if pos + 5 > len(clx) or clx[pos] != 2:
            raise ValueError("DOC 文本结构无法识别，请另存 DOCX")
        size = struct.unpack_from("<I", clx, pos + 1)[0]
        plc = clx[pos + 5:pos + 5 + size]
        if len(plc) != size or size < 4 or (size - 4) % 12:
            raise ValueError("DOC 文本表损坏")
        count = (size - 4) // 12
        cps = struct.unpack_from("<" + "I" * (count + 1), plc)
        pieces = []
        for i in range(count):
            if cps[i] >= ccp: break
            chars = min(cps[i + 1], ccp) - cps[i]
            if chars < 0 or chars > 200000:
                raise ValueError("DOC 文本长度异常")
            packed = struct.unpack_from("<I", plc, (count + 1) * 4 + i * 8 + 2)[0]
            compressed = bool(packed & 0x40000000)
            offset = packed & 0x3FFFFFFF
            offset = offset // 2 if compressed else offset
            end = offset + chars * (1 if compressed else 2)
            if end > len(word): raise ValueError("DOC 文本偏移异常")
            pieces.append(word[offset:end].decode("cp1252" if compressed else "utf-16-le", errors="replace"))
        return "".join(pieces).replace("\r", "\n").replace("\x07", "\n")


def import_file(filename, binary):
    if len(binary) > 10_000_000:
        raise ValueError("文献清单文件限 10 MB；Word 中的图片不会用于识别")
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(binary)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 10_000_000: raise ValueError("DOCX 文字部分过大")
            xml = archive.read(info)
        if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml: raise ValueError("不支持含外部实体的文档")
        root = ET.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = ["".join(t.text or "" for t in p.iter(ns + "t")) for p in root.iter(ns + "p")]
        return parse_references("\n".join(paragraphs))
    if suffix == ".doc":
        try:
            return parse_references(doc_text(binary))
        except (OSError, struct.error, KeyError):
            raise ValueError("DOC 无法提取文字；请另存为 DOCX 后导入") from None
    for encoding in (["utf-16"] if binary.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]):
        try:
            text = binary.decode(encoding)
            break
        except UnicodeDecodeError:
            text = None
    if text is None: raise ValueError("无法识别文本编码，请使用 UTF-8 CSV")
    if suffix != ".csv":
        if suffix not in {".txt", ".tsv", ".ris", ".bib", ".nbib"}:
            raise ValueError("支持 CSV、DOC、DOCX、TXT、TSV、RIS、BibTeX 和 NBIB")
        return parse_references(text)
    try: dialect = csv.Sniffer().sniff(text[:10000], delimiters=",;\t")
    except csv.Error: dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows: return []
    aliases = {"doi": "doi", "文献doi": "doi", "digitalobjectidentifier": "doi", "title": "title", "articletitle": "title", "标题": "title", "题名": "title", "authors": "authors", "author": "authors", "作者": "authors"}
    header = [aliases.get(re.sub(r"[\s_-]", "", c).lower()) for c in rows[0]]
    records = []
    for row in rows[1:] if any(header) else rows:
        if not any(c.strip() for c in row): continue
        if any(header):
            fields = {key: row[i] for i, key in enumerate(header) if key and i < len(row)}
            records.append(make_record(fields.get("title", ""), split_authors(fields.get("authors", "")), fields.get("doi", ""), "CSV"))
        else:
            records.extend(parse_references("\t".join(row)))
        if len(records) > 200: raise ValueError("每次最多导入 200 篇")
    return [r for r in records if r["title"] or r["doi"]]
