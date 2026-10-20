"""Deterministic, offline reference parsing; never invent missing fields."""
import re
from core import DOI_RE, doi_value


def split_authors(value):
    return [a.strip().strip(";, ") for a in re.split(r"\s+and\s+|[;；]|\s*&\s*", value, flags=re.I) if a.strip()][:300]


def make_record(title="", authors=None, doi="", source="text"):
    title = re.sub(r"\s+", " ", title).strip(" \t\n.{}\"")
    return dict(title=title[:2000], authors=authors or [], doi=doi_value(doi), format=source,
                needs_review=not (title and doi), parser="local_rules")


def bib_fields(text):
    """Read quoted/braced fields, including nested braces in a title."""
    fields = {}
    cursor = 0
    while match := re.search(r"\b(doi|title|author)\s*=\s*", text[cursor:], re.I):
        key = match[1].lower()
        start = cursor + match.end()
        if start >= len(text):
            break
        opener = text[start]
        end = start
        if opener == "{":
            depth = 1
            end += 1
            while end < len(text) and depth:
                if text[end] == "{": depth += 1
                if text[end] == "}": depth -= 1
                end += 1
            value = text[start + 1:end - 1]
        elif opener == '"':
            end = start + 1
            while end < len(text) and (text[end] != '"' or text[end - 1] == "\\"):
                end += 1
            value = text[start + 1:end]
            end += 1
        else:
            end = text.find(",", start)
            end = len(text) if end < 0 else end
            value = text[start:end]
        fields[key] = value.replace("{", "").replace("}", "")
        cursor = max(start + 1, end)
    return fields


def parse_block(block):
    fields = {}
    authors = []
    source = "text"
    if re.search(r"(?m)^\s*(?:TY|TI|T1|FAU)\s+-\s", block):
        source = "RIS/NBIB"
        current = ""
        for line in block.splitlines():
            tagged = re.match(r"^\s*([A-Z0-9]{2,4})\s+-\s*(.*)$", line)
            if tagged:
                current, value = tagged.groups()
                if current in {"AU", "A1", "FAU"}:
                    authors.append(value.strip())
                elif current in {"TI", "T1", "DO"}:
                    fields[current] = value.strip()
            elif current in {"TI", "T1"} and line.startswith(" "):
                fields[current] = fields.get(current, "") + " " + line.strip()
        return make_record(fields.get("TI", fields.get("T1", "")), list(dict.fromkeys(authors)), fields.get("DO") or doi_value(block), source)
    if re.match(r"\s*@\w+\s*[{(]", block):
        fields = bib_fields(block)
        return make_record(fields.get("title", ""), split_authors(fields.get("author", "")), fields.get("doi", ""), "BibTeX")
    title_match = re.search(r"(?im)^\s*(?:标题|题名|title)\s*[:：]\s*(.+)$", block)
    author_match = re.search(r"(?im)^\s*(?:作者|authors?)\s*[:：]\s*(.+)$", block)
    doi = doi_value(block)
    if title_match:
        return make_record(title_match[1], split_authors(author_match[1]) if author_match else [], doi, "labeled")
    # APA-like author (year). Title. Journal ...
    citation = re.match(r"\s*(.+?)\s*\((?:19|20)\d{2}[a-z]?\)\s*[.。]\s*(.+?)(?:[.。]\s|$)", block, re.S)
    if citation:
        return make_record(citation[2], split_authors(citation[1]), doi, "citation")
    candidates = []
    for line in block.splitlines():
        if re.match(r"(?i)^\s*(?:authors?|作者)\s*[:：]", line):
            continue
        line = re.sub(r"https?://\S+", "", line)
        line = DOI_RE.sub("", line)
        line = re.sub(r"(?i)^\s*(?:doi\s*[:：]?|authors?\s*[:：]|作者\s*[:：])\s*", "", line).strip(" \t;；,，.。")
        if line and not re.fullmatch(r"[\d\W]+", line):
            candidates.append(line)
    authors = split_authors(author_match[1]) if author_match else []
    author_line = ""
    if not authors:
        for line in candidates:
            names = split_authors(line)
            if len(names) > 1 and all(2 <= len(n.split()) <= 4 and all(w[0].isupper() for w in n.split() if w) for n in names):
                authors = names
                author_line = line
                break
    title = max((line for line in candidates if line != author_line), key=len, default="")
    return make_record(title, authors, doi)


def split_blocks(text):
    if not isinstance(text, str) or len(text) > 200000:
        raise ValueError("请粘贴 20 万字符以内的文献信息")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if re.search(r"(?m)^\s*@\w+\s*[{(]", text):
        blocks = re.split(r"(?m)(?=^\s*@\w+\s*[{(])", text)
    elif re.search(r"(?m)^\s*TY\s+-", text):
        blocks = re.split(r"(?m)(?=^\s*TY\s+-)", text)
    elif re.search(r"(?m)^\s*PMID\s*-", text):
        blocks = re.split(r"(?m)(?=^\s*PMID\s*-)", text)
    else:
        blocks = re.split(r"\n\s*\n", text)
        expanded = []
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if len(lines) > 1 and len({doi_value(line) for line in lines if doi_value(line)}) > 1 and not all(doi_value(line) for line in lines):
                pending = []
                for line in lines:
                    pending.append(line)
                    if doi_value(line):
                        expanded.append("\n".join(pending))
                        pending = []
                if pending:
                    expanded.append("\n".join(pending))
            elif len(lines) > 1 and (all(doi_value(line) for line in lines) or all(not doi_value(line) for line in lines) and not any(re.match(r"(?i)^(?:title|author|标题|作者)[:：]", line) for line in lines)):
                expanded.extend(lines)
            else:
                expanded.append(block)
        blocks = expanded
    return blocks


def parse_references(text):
    blocks = split_blocks(text)
    records, seen = [], set()
    for block in blocks:
        if not block.strip():
            continue
        record = parse_block(block)
        if not (record["title"] or record["doi"]):
            continue
        key = record["doi"] or record["title"].casefold()
        if key not in seen:
            records.append(record)
            seen.add(key)
        if len(records) > 200:
            raise ValueError("每次最多识别 200 篇，请分批粘贴")
    return records


def unparsed_segments(text):
    """Blocks the local rules could not turn into a usable record, so an external harness model can read them.
    reason: no_title_no_doi (dropped by the importer) | partial (has a title or a DOI but not both)."""
    out = []
    for index, block in enumerate(split_blocks(text)):
        if not block.strip():
            continue
        record = parse_block(block)
        if not (record["title"] or record["doi"]):
            out.append(dict(index=index, reason="no_title_no_doi", text=block.strip()[:600]))
        elif record["needs_review"]:
            out.append(dict(index=index, reason="partial", text=block.strip()[:600],
                            parsed=dict(title=record["title"][:200], doi=record["doi"])))
    return out
