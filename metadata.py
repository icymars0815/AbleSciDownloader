"""Public DOI metadata only. No PDF text, account data, cookies, or AI requests."""
import hashlib
import html
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core import doi_value, now, verify_pdf

HOSTS = {"api.openalex.org", "api.crossref.org"}
_ctx = threading.local()   # contact email of the current lookup (optional polite pool); never logged or stored
RETRACTION_TYPES = ("retraction", "withdrawal", "removal", "expression_of_concern")
CORRECTION_TYPES = ("correction", "erratum", "corrigendum", "addendum", "clarification")


class MetadataRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https" or urlparse(newurl).netloc not in HOSTS:
            raise ValueError("metadata redirect outside public API")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get_json(url):
    if urlparse(url).netloc not in HOSTS:
        raise ValueError("unknown metadata host")
    email = re.sub(r"[^\w.@+\-]", "", getattr(_ctx, "email", "") or "")
    agent = "KYTDL/1.0 (mailto:%s)" % email if email else "AbleSciLocalDesk/0.1"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": agent})
    with build_opener(MetadataRedirects()).open(request, timeout=8) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("metadata response too large")
    return json.loads(data)


def reconstruct_abstract(index):
    if not isinstance(index, dict):
        return ""
    words = {}
    for word, positions in index.items():
        for position in positions if isinstance(positions, list) else []:
            if isinstance(position, int) and 0 <= position < 20000:
                words[position] = str(word)
    return " ".join(words[p] for p in sorted(words))[:30000]


def _kind(text):
    low = re.sub(r"[\s-]+", "_", str(text or "").strip().lower())
    for name in RETRACTION_TYPES:
        if name in low:
            return name
    if "retract" in low:
        return "retraction"
    for name in CORRECTION_TYPES:
        if name in low:
            return "correction"
    return low[:40] or "update"


def crossref_retraction(message):
    """Updates the Crossref record declares: this paper updated by notices (role 'paper'), or this record is
    itself a notice for another work (role 'notice'). Only 'paper' retractions mark the paper retracted."""
    updates = []

    def add(kind, target, role, label=""):
        entry = dict(type=_kind(kind), doi=doi_value(str(target or "")), role=role, source="Crossref")
        if label:
            entry["label"] = str(label)[:120]
        if entry not in updates:
            updates.append(entry)

    for field, role in (("updated-by", "paper"), ("update-to", "notice")):
        for item in message.get(field) or []:
            if isinstance(item, dict):
                add(item.get("type") or item.get("label"), item.get("DOI"), role, item.get("label", ""))
    relation = message.get("relation")
    for key, items in (relation.items() if isinstance(relation, dict) else []):
        low = str(key).lower()
        if not any(t in low for t in ("retract", "correct", "erratum", "withdraw", "concern")):
            continue
        role = "notice" if low.startswith(("is-retraction-of", "is-correction-of", "is-erratum-for")) or low in ("is-correction-of", "is-retraction-of") else "paper"
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                add(key, item.get("id"), role)
    retracted = True if any(u["role"] == "paper" and u["type"] in RETRACTION_TYPES for u in updates) else None
    return dict(retracted=retracted, updates=updates, source="Crossref")


def merge_retraction(parts):
    """Combine per-source flags: any True wins, else an explicit False, else None (unknown). Never guesses."""
    parts = [p for p in parts if isinstance(p, dict)]
    values = [p.get("retracted") for p in parts]
    updates = []
    for part in parts:
        for update in part.get("updates") or []:
            if update not in updates:
                updates.append(update)
    retracted = True if True in values else False if False in values else None
    return dict(retracted=retracted, updates=updates, source="+".join(p["source"] for p in parts if p.get("retracted") is not None or p.get("updates")) or None)


def openalex_record(raw):
    flag = raw.get("is_retracted")
    return dict(retraction=dict(retracted=flag if isinstance(flag, bool) else None, updates=[], source="OpenAlex"), doi=doi_value(raw.get("doi", "")), title=str(raw.get("title") or "")[:2000],
                authors=[str(a.get("author", {}).get("display_name", ""))[:200] for a in raw.get("authorships", [])][:300],
                abstract=reconstruct_abstract(raw.get("abstract_inverted_index")),
                year=raw.get("publication_year"), source="OpenAlex")


def crossref_record(raw):
    raw = raw.get("message", {})
    titles = raw.get("title") or []
    return dict(retraction=crossref_retraction(raw), doi=doi_value(raw.get("DOI", "")), title=html.unescape(titles[0])[:2000] if titles else "",
                authors=[" ".join(filter(None, [a.get("given", ""), a.get("family", "")])) or a.get("name", "") for a in raw.get("author", [])][:300],
                abstract=html.unescape(re.sub(r"<[^>]*>", " ", raw.get("abstract") or ""))[:30000],
                source="Crossref")


def fetch_metadata(doi, cache_root, email=""):
    """Public bibliographic record plus retraction/correction flags. email (optional) adds mailto= to the OpenAlex and
    Crossref requests and to the User-Agent (polite pool); empty keeps the requests anonymous."""
    old = getattr(_ctx, "email", "")
    _ctx.email = (email or "").strip()
    try:
        return _fetch_metadata(doi, cache_root, _ctx.email)
    finally:
        _ctx.email = old


def _fetch_metadata(doi, cache_root, email):
    doi = doi_value(doi)
    if not doi:
        return dict(status="unavailable", reason="未提供 DOI，作者和摘要未核实")
    folder = Path(cache_root) / "metadata"
    folder.mkdir(parents=True, exist_ok=True)
    cache = folder / (hashlib.sha256(doi.encode()).hexdigest() + ".json")
    try:
        saved = json.loads(cache.read_text("utf-8"))
        ttl = 86400 if saved.get("status") == "available" else 900
        if saved.get("doi") == doi and time.time() - saved.get("cached_at", 0) < ttl:
            return dict(saved, cached=True)
    except (OSError, ValueError, TypeError):
        pass
    encoded = quote(doi, safe="/")
    mail = quote(email, safe="@")
    providers = [("OpenAlex", "https://api.openalex.org/works/https://doi.org/" + encoded +
                  "?select=doi,title,authorships,abstract_inverted_index,publication_year,is_retracted" + ("&mailto=" + mail if mail else ""), openalex_record),
                 ("Crossref", "https://api.crossref.org/works/" + encoded + ("?mailto=" + mail if mail else ""), crossref_record)]
    result = dict(doi=doi, status="unavailable", sources=[], errors=[], field_sources={}, fetched_at=now(), cached_at=time.time())
    retraction_parts = []
    for source, url, parse in providers:
        try:
            record = parse(get_json(url))
            if record["doi"] != doi:
                raise ValueError("DOI mismatch")
            retraction_parts.append(record.get("retraction"))
            result["sources"].append(source)
            result["status"] = "available"
            for field in ("title", "authors", "abstract", "year"):
                if record.get(field) and not result.get(field):
                    result[field] = record[field]
                    result["field_sources"][field] = source
        except Exception as exc:
            # Never store a raw URL, third-party response, or proxy credentials.
            status = getattr(exc, "code", None)
            if not (source == "Crossref" and all(result.get(field) for field in ("title", "authors", "abstract"))):
                result["errors"].append(source + (f" HTTP {status}" if status else " 请求未完成或记录无效"))
    if result["status"] == "available":
        result["retraction"] = merge_retraction(retraction_parts)
    if result["status"] != "available":
        result["reason"] = "公开书目信息暂不可用，作者和摘要未核实"
    # Atomic cache write; concurrent requests never read partial JSON.
    import uuid
    temp = cache.with_suffix("." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False), "utf-8")
    temp.replace(cache)
    return result


def verify_with_metadata(path, title, doi, cache_root, lookup=True, email=""):
    metadata = fetch_metadata(doi, cache_root, email) if lookup else None
    return verify_pdf(path, title, doi, metadata=metadata)
