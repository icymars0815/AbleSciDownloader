"""Legal open-access PDF lookup and download. Public APIs and publisher/repository URLs only.
No cookies, no credentials, no 科研通 access. Sci-Hub or any shadow library is deliberately not supported."""
import hashlib
import ipaddress
import json
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core import doi_value, now

USER_AGENT = "KYTDL/1.0 (local literature helper)"
CONTACT_EMAIL_HELP = ("可选。填写后，该邮箱会随请求发送给 OpenAlex、Crossref 和 Unpaywall（进入「礼貌池」，限速更宽松、"
                      "对方出问题时可联系你）；留空则匿名访问，不发送任何邮箱。")
POLITE_HOSTS = ("api.openalex.org", "api.crossref.org")
LANDING_MAX = 2 * 1024 * 1024   # bytes of a landing page that are read
RETRY_CODES = (429, 500, 503)
MAX_RETRY_WAIT = 10.0           # a Retry-After above this skips the source instead of waiting
BACKOFF = 2.0                   # the single retry for 500/503 without Retry-After
MAX_PER_SOURCE = 6
CACHE_HIT_TTL = 7 * 86400
CACHE_MISS_TTL = 86400
_ctx = threading.local()        # per-lookup settings (contact email, CORE key); never logged
_wall = time.time               # tests replace this with a fake clock
MAX_BYTES = 60 * 1024 * 1024
MAX_JSON = 4 * 1024 * 1024
TIMEOUT = 45
TOTAL_CAP = 120       # seconds for one whole oa_fetch
BROWSER_CAP = 60      # seconds for the single headless browser fallback
HUMAN_CAP = 300       # seconds a visible window waits for a person to pass a publisher check
BLOCK_CODES = (403, 429, 503)
HOST_GAP = 1.0        # seconds between two requests to the same host
_host_lock = threading.Lock()
_host_next = {}       # host -> earliest monotonic time of the next request
_clock, _sleep = time.monotonic, time.sleep   # tests replace these with a fake clock


def _throttle(url):
    """Keep >= HOST_GAP seconds between requests to one host (slots are reserved, so threads cannot bunch up)."""
    host = urlparse(url).netloc.lower()
    with _host_lock:
        now = _clock()
        start = max(now, _host_next.get(host, 0.0))
        _host_next[host] = start + HOST_GAP
    if start > now:
        _sleep(start - now)


STRONG_MARKERS = ("just a moment", "cf-challenge", "challenges.cloudflare.com", "turnstile", "verify you are human")
WEAK_MARKERS = ("captcha", "g-recaptcha", "hcaptcha", "attention required")


def challenge_in(text, status=None):
    """True if a page/response looks like a bot check (Cloudflare, Turnstile, CAPTCHA). We never try to solve it."""
    text = str(text or "").lower()
    if any(m in text for m in STRONG_MARKERS):
        return True
    return (status is None or status in BLOCK_CODES) and any(m in text for m in WEAK_MARKERS)


class OAError(Exception):
    def __init__(self, message, status=None, challenge=False):
        super().__init__(message)
        self.status = status
        self.challenge = challenge
        self.retry_after = None


class OAChallenge(OAError):
    def __init__(self, message="publisher check"):
        super().__init__(message, None, True)


def _retry_after(value):
    """Seconds from a Retry-After header (delta-seconds or HTTP date), or None."""
    value = str(value or "").strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - _wall())
    except Exception:
        return None


class _Redirects(HTTPRedirectHandler):
    max_redirections = 10   # per request
    max_repeats = 2         # the same address at most twice

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme not in ("http", "https"):
            raise OAError("redirect to a non-http(s) address")
        if is_private_url(newurl):
            raise OAError("redirect to a local or private address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


TOTAL_DEADLINE = 60     # seconds for one whole request including the body (the socket timeout only bounds each read)


def is_private_url(url):
    """True for loopback / private / link-local / unspecified hosts. Third-party metadata must never make this app
    contact the local machine or LAN. Literal addresses and well-known local names only (no DNS lookup here)."""
    try:
        host = (urlparse(url).hostname or "").strip("[]").lower()
    except ValueError:
        return True
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def _request(url, accept="*/*", max_bytes=MAX_BYTES, timeout=TIMEOUT):
    """The single network entry point (tests patch this). No cookie processor is installed, so none are kept or sent."""
    if urlparse(url).scheme not in ("http", "https"):
        raise OAError("not an http(s) address")
    if is_private_url(url):
        raise OAError("local or private address skipped")
    host = urlparse(url).netloc.lower()
    agent = USER_AGENT
    email = re.sub(r"[^\w.@+\-]", "", getattr(_ctx, "email", "") or "")
    if email and host in POLITE_HOSTS:
        agent = "KYTDL/1.0 (mailto:%s)" % email
    headers = {"User-Agent": agent, "Accept": accept}
    if host == "api.core.ac.uk" and getattr(_ctx, "core_key", ""):
        headers["Authorization"] = "Bearer " + _ctx.core_key
    request = Request(url, headers=headers)
    _throttle(url)
    started = time.monotonic()
    try:
        with build_opener(_Redirects()).open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise OAError("file too large", response.status)
            chunks, size = [], 0
            while size <= max_bytes:
                if time.monotonic() - started > TOTAL_DEADLINE:
                    raise OAError("request deadline exceeded")
                chunk = response.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b"".join(chunks)
            status = response.status
    except HTTPError as exc:
        try:
            head = exc.read(65536).decode("latin-1")
        except Exception:
            head = ""
        error = OAError("HTTP %d" % exc.code, exc.code, challenge_in(head, exc.code) or exc.headers.get("cf-mitigated") == "challenge")
        error.retry_after = _retry_after(exc.headers.get("Retry-After"))
        raise error
    except OAError:
        raise
    except Exception as exc:
        raise OAError(type(exc).__name__)
    if len(body) > max_bytes:
        raise OAError("file too large", status)
    return status, body


def _get(url, accept, max_bytes, timeout, deadline=None, record=None):
    """_request with at most one retry: 429/500/503 only, never after a bot check. A Retry-After above
    MAX_RETRY_WAIT skips the source; without the header only 500/503 are retried (after a short backoff)."""
    try:
        return _request(url, accept, max_bytes, timeout)
    except OAError as exc:
        if exc.challenge or exc.status not in RETRY_CODES:
            raise
        wait = exc.retry_after
        if wait is None:
            if exc.status == 429:
                raise
            wait = BACKOFF
        if wait > MAX_RETRY_WAIT or (deadline and time.monotonic() + wait + 1 >= deadline):
            raise
        _sleep(wait)
        if record is not None:
            record["retried"] = True
        return _request(url, accept, max_bytes, timeout)


TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "oly_", "vero_")
TRACKING_NAMES = {"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "gbraid", "wbraid", "_hsenc", "_hsmi", "mkt_tok",
                  "ref_src", "spm", "cmpid", "_ga", "_gl", "trk", "s_cid", "sr_share"}
KEEP_PARAMS = {"download", "pdf", "type", "id", "article", "file", "render", "version", "doi", "format", "name",
               "sequence", "isallowed", "pid", "uri", "key", "token", "filename", "view", "mode"}


def normalize_url(url):
    """Canonical form for deduping: http -> https (never the reverse; IPs, localhost and custom ports are left alone),
    lower-case host, no default port or fragment, tracking parameters removed but file-selecting ones kept.
    Returns None for anything that is not an http(s) address."""
    if not isinstance(url, str):
        return None
    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host, scheme = parts.netloc.lower(), parts.scheme
    if host.endswith(":80") and scheme == "http":
        host = host[:-3]
    if host.endswith(":443") and scheme == "https":
        host = host[:-4]
    if scheme == "http" and not re.fullmatch(r"[\d.]+|localhost|\[.*\]", host) and ":" not in host:
        scheme = "https"
    kept = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        low = name.lower()
        if low not in KEEP_PARAMS and (low in TRACKING_NAMES or low.startswith(TRACKING_PREFIXES)):
            continue
        kept.append((name, value))
    return urlunparse((scheme, host, parts.path or "/", parts.params, urlencode(kept, quote_via=quote), ""))


def url_key(url):
    """Dedup key: the normalized address with its query parameters sorted."""
    norm = normalize_url(url)
    if not norm:
        return None
    parts = urlparse(norm)
    return urlunparse((parts.scheme, parts.netloc, parts.path, parts.params, urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), quote_via=quote), ""))


def _version(value):
    low = re.sub(r"[\s_-]", "", str(value or "")).lower()
    for name in ("published", "accepted", "submitted"):
        if low in (name, name + "version"):
            return name + "Version"
    return None


class _LandingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls, self.base = [], None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "").strip() for k, v in attrs}
        if tag == "meta":
            if a.get("name", "").lower() == "citation_pdf_url" and a.get("content"):
                self.urls.append(a["content"])
            elif a.get("http-equiv", "").lower() == "refresh":
                found = re.search(r"url\s*=\s*['\"]?([^'\";]+)", a.get("content", ""), re.I)
                if found:
                    self.urls.append(found.group(1).strip())
        elif tag == "link":
            if "alternate" in a.get("rel", "").lower().split() and a.get("type", "").lower().startswith("application/pdf") and a.get("href"):
                self.urls.append(a["href"])
        elif tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]


def parse_landing(html_text, base_url):
    """PDF addresses advertised by a landing page: citation_pdf_url, <link rel=alternate type=application/pdf>, meta refresh.
    Absolute, normalized, de-duplicated, in page order. Only the first 2 MB are parsed."""
    parser = _LandingParser()
    try:
        parser.feed(str(html_text or "")[:LANDING_MAX])
        parser.close()
    except Exception:
        pass
    base = urljoin(base_url, parser.base) if parser.base else base_url
    out, seen = [], set()
    for raw in parser.urls:
        url = normalize_url(urljoin(base, raw))
        key = url_key(url) if url else None
        if key and key not in seen:
            seen.add(key)
            out.append(url)
    return out


def discover_landing(info, deadline, skip_hosts=()):
    """Plain GET (no cookies, throttled; a bot check is recorded and left alone) of the landing pages of a confirmed-OA item.
    Returns ([{url, source: 'landing'}], attempts)."""
    attempts, found = [], []
    for page in (info.get("landing_urls") or [])[:2]:
        host = _host(page)
        record = dict(kind="landing", source="landing", host=host, status=None, bytes=0, ok=False)
        attempts.append(record)
        if host in skip_hosts:
            record["status"] = "skipped (429)"
            continue
        if _expired(deadline):
            record["status"] = "time cap"
            break
        try:
            status, body = _get(page, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", LANDING_MAX, min(15, _left(deadline)), deadline, record)
        except OAError as exc:
            record["status"] = exc.status or str(exc)[:60]
            if exc.challenge:
                record["challenge"] = True
            continue
        record.update(status=status, bytes=len(body))
        if body.startswith(b"%PDF-"):
            found.append(dict(url=normalize_url(page), source="landing"))
            record["ok"] = True
            continue
        text = body.decode("utf-8", "replace")
        if challenge_in(text[:65536], 200):
            record.update(status="publisher check", challenge=True)
            continue
        urls = parse_landing(text, page)
        record.update(ok=True, found=len(urls))
        found.extend(dict(url=u, source="landing") for u in urls)
    return found[:MAX_PER_SOURCE], attempts


def _cache_file(root, doi):
    return Path(root) / "cache" / "oa" / (hashlib.sha256(doi.encode()).hexdigest() + ".json")


def _cache_read(root, doi):
    try:
        saved = json.loads(_cache_file(root, doi).read_text("utf-8"))
        ttl = CACHE_HIT_TTL if saved["result"].get("candidates") else CACHE_MISS_TTL
        if saved.get("doi") == doi and 0 <= _wall() - saved["cached_at"] < ttl:
            return dict(saved["result"], cached=True, fetched_at=saved.get("fetched_at"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def _cache_write(root, doi, result):
    """Hits (candidates found) and clean misses (not open access) are cached; failed or partial lookups are not."""
    if not result.get("lookup_ok"):
        return
    if not result["candidates"]:
        if result.get("is_oa") or any(not a.get("ok") and a.get("status") != 404 for a in result["attempts"]):
            return
    path = _cache_file(root, doi)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(dict(doi=doi, cached_at=_wall(), fetched_at=now(), result=result), ensure_ascii=False), "utf-8")
        temp.replace(path)
    except (OSError, TypeError):
        pass


def clear_oa_cache(root):
    """Delete the cached OA lookups under <root>/cache/oa. Returns how many files were removed."""
    folder = Path(root) / "cache" / "oa"
    count = 0
    for path in folder.glob("*.json") if folder.is_dir() else []:
        try:
            path.unlink()
            count += 1
        except OSError:
            pass
    return count


def _host(url):
    return urlparse(url).netloc.lower()[:80]


def _left(deadline):
    return max(1.0, deadline - time.monotonic()) if deadline else TIMEOUT


def _expired(deadline):
    return bool(deadline) and time.monotonic() >= deadline


def _pmcid(text):
    found = re.search(r"(?:/pmc/articles/|pmc\.ncbi\.nlm\.nih\.gov/articles/|europepmc\.org/(?:articles|article/PMC)/)(?:PMC)?(\d{4,9})", str(text or ""), re.I)
    if found:
        return "PMC" + found.group(1)
    found = re.fullmatch(r"\s*(?:PMC)?(\d{4,9})\s*", str(text or ""), re.I)
    return "PMC" + found.group(1) if found else ""


def _web_url(url):
    return url.strip() if isinstance(url, str) and urlparse(url.strip()).scheme in ("http", "https") else None


def _tier(url, repository=False):
    host = _host(url)
    if host.endswith("europepmc.org") or host.endswith("ncbi.nlm.nih.gov"):
        return 1
    if repository or host.endswith(("arxiv.org", "biorxiv.org", "medrxiv.org", "zenodo.org", "osf.io")):
        return 2
    return 3


def find_oa_locations(doi, email="", deadline=None, core_api_key="", cache_root=None):
    """Return dict(candidates=[{url, source, version}], is_oa, oa_status, license, version, pmcid, attempts, lookup_ok, landing_urls).
    Candidates are ordered: publisher OA pdf, PMC / Europe PMC, repositories and preprints, others.
    email (optional) joins the OpenAlex/Crossref polite pool; core_api_key enables CORE; cache_root enables the disk cache."""
    doi = doi_value(doi)
    email, core_api_key = (email or "").strip(), (core_api_key or "").strip()
    if doi and cache_root:
        cached = _cache_read(cache_root, doi)
        if cached is not None:
            return cached
    old = (getattr(_ctx, "email", ""), getattr(_ctx, "core_key", ""))
    _ctx.email, _ctx.core_key = email, core_api_key
    try:
        result = _find(doi, email, deadline, core_api_key)
    finally:
        _ctx.email, _ctx.core_key = old
    if doi and cache_root:
        _cache_write(cache_root, doi, result)
    return result


def _find(doi, email, deadline, core_key):
    found = []     # (tier, order, url, source, version)
    attempts = []
    info = dict(is_oa=False, oa_status=None, license=None, pmcid="", ok=False, manual_url=None, landing=[])

    def add(url, source, tier=None, repository=False, version=None):
        if not isinstance(url, str):
            return
        url = url.strip()
        if url.startswith("ftp://ftp.ncbi.nlm.nih.gov/"):
            url = "https://" + url[6:]
        url = normalize_url(url)
        if not url:
            return
        found.append((_tier(url, repository) if tier is None else tier, len(found), url, source, _version(version)))

    limited = set()   # hosts that answered 429 (after the one allowed retry) during this lookup: skipped from then on

    def lookup(source, url, xml=False):
        record = dict(kind="lookup", source=source, host=_host(url), status=None, bytes=0, ok=False)
        attempts.append(record)
        if _host(url) in limited:
            record["status"] = "skipped (429)"
            return None
        if _expired(deadline):
            record["status"] = "time cap"
            return None
        try:
            status, body = _get(url, "application/xml" if xml else "application/json", MAX_JSON, min(15, _left(deadline)), deadline, record)
            data = ET.fromstring(body) if xml else json.loads(body)
            record.update(status=status, bytes=len(body), ok=True)
            return data
        except OAError as exc:
            if exc.status == 429 or (source == "CORE" and exc.status in (401, 403)):
                limited.add(_host(url))
            record["status"] = exc.status or str(exc)[:60]
        except Exception:
            record["status"] = "invalid response"
        return None

    def set_pmcid(value):
        value = _pmcid(value)
        if value and not info["pmcid"]:
            info["pmcid"] = value

    if not doi:
        return dict(candidates=[], attempts=attempts, pmcid="", is_oa=False, oa_status=None, license=None, lookup_ok=False, landing_urls=[], version=None)
    encoded = quote(doi, safe="/")

    # 1. OpenAlex
    polite = "&mailto=" + quote(email, safe="@") if email else ""
    work = lookup("OpenAlex", "https://api.openalex.org/works/doi:" + encoded + "?select=doi,open_access,best_oa_location,locations,ids" + polite)
    if isinstance(work, dict):
        info["ok"] = True
        oa = work.get("open_access") or {}
        info["is_oa"] = bool(oa.get("is_oa"))
        info["oa_status"] = oa.get("oa_status")
        best = work.get("best_oa_location") or {}
        info["license"] = best.get("license")
        info["manual_url"] = best.get("landing_page_url") or oa.get("oa_url") or best.get("pdf_url")
        add(best.get("pdf_url"), "OpenAlex", 0, version=best.get("version"))
        if info["is_oa"] and _web_url(best.get("landing_page_url")) and not _pmcid(best.get("landing_page_url")):
            info["landing"].append(best["landing_page_url"])
        set_pmcid(best.get("landing_page_url"))
        set_pmcid((work.get("ids") or {}).get("pmcid"))
        for loc in work.get("locations") or []:
            if not isinstance(loc, dict):
                continue
            set_pmcid(loc.get("landing_page_url"))
            repository = (loc.get("source") or {}).get("type") == "repository"
            add(loc.get("pdf_url"), "OpenAlex", repository=repository, version=loc.get("version"))

    # 2. Europe PMC search (also finds a PMCID that OpenAlex lacks)
    epmc = lookup("Europe PMC", 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=' + quote('DOI:"' + doi + '"') + '&format=json&resultType=core')
    if isinstance(epmc, dict):
        info["ok"] = True
        results = ((epmc.get("resultList") or {}).get("result")) or []
        record = next((r for r in results if isinstance(r, dict) and doi_value(r.get("doi", "")) == doi), None)
        if record:
            set_pmcid(record.get("pmcid"))
            if record.get("isOpenAccess") == "Y":
                info["is_oa"] = True
            for entry in ((record.get("fullTextUrlList") or {}).get("fullTextUrl")) or []:
                if entry.get("documentStyle") == "pdf" and entry.get("availabilityCode") == "OA":
                    add(entry.get("url"), "Europe PMC")
    # 3. NCBI ID converter, only when no PMCID is known yet
    if not info["pmcid"]:
        conv = lookup("NCBI idconv", "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids=" + encoded + "&format=json")
        if isinstance(conv, dict):
            info["ok"] = True
            for record in conv.get("records") or []:
                set_pmcid(record.get("pmcid") if isinstance(record, dict) else "")
    if info["pmcid"]:
        add("https://europepmc.org/articles/%s?pdf=render" % info["pmcid"], "Europe PMC", 1)
        add("https://pmc.ncbi.nlm.nih.gov/articles/%s/pdf/" % info["pmcid"], "PMC", 1)
        # 4. PMC OA web service: direct pdf links only (tgz packages are skipped)
        tree = lookup("PMC OA", "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=" + info["pmcid"], xml=True)
        if tree is not None:
            for link in tree.iter("link"):
                if link.get("format") == "pdf":
                    add(link.get("href"), "PMC OA", 1)
    # 5. Semantic Scholar (no key; a 429 is just recorded)
    s2 = lookup("Semantic Scholar", "https://api.semanticscholar.org/graph/v1/paper/DOI:" + encoded + "?fields=openAccessPdf,externalIds")
    if isinstance(s2, dict):
        info["ok"] = True
        pdf = (s2.get("openAccessPdf") or {}).get("url")
        if pdf:
            info["is_oa"] = True
            add(pdf, "Semantic Scholar")
        set_pmcid((s2.get("externalIds") or {}).get("PubMedCentral"))
    # 6. Unpaywall only when the user supplied a contact email
    if email:
        up = lookup("Unpaywall", "https://api.unpaywall.org/v2/" + encoded + "?email=" + quote(email, safe="@"))
        if isinstance(up, dict):
            info["ok"] = True
            info["is_oa"] = info["is_oa"] or bool(up.get("is_oa"))
            info["oa_status"] = info["oa_status"] or up.get("oa_status")
            for loc in [up.get("best_oa_location")] + list(up.get("oa_locations") or []):
                if isinstance(loc, dict):
                    info["license"] = info["license"] or loc.get("license")
                    add(loc.get("url_for_pdf"), "Unpaywall", repository=loc.get("host_type") == "repository", version=loc.get("version"))
    # 6b. CORE (repositories) only when the user supplied an API key; a 401/403 drops it for this lookup
    if core_key:
        core = lookup("CORE", "https://api.core.ac.uk/v3/search/works?q=" + quote('doi:"%s"' % doi) + "&limit=3")
        if isinstance(core, dict):
            info["ok"] = True
            for record in core.get("results") or []:
                if isinstance(record, dict) and doi_value(record.get("doi") or "") == doi:
                    version = record.get("version") or record.get("publishedVersion") or record.get("oaiVersion")
                    for link in [record.get("downloadUrl")] + list(record.get("sourceFulltextUrls") or []):
                        if isinstance(link, str) and link.strip():
                            info["is_oa"] = True
                            add(link, "CORE", repository=True, version=version)
    # 7. Preprint servers
    arxiv = re.fullmatch(r"10\.48550/arxiv\.(.+)", doi)
    if arxiv:
        info["is_oa"] = True
        add("https://arxiv.org/pdf/" + arxiv.group(1), "arXiv", 2)
    if doi.startswith("10.1101/"):
        for server in ("biorxiv", "medrxiv"):
            data = lookup(server, "https://api.biorxiv.org/details/%s/%s" % (server, encoded))
            entries = data.get("collection") if isinstance(data, dict) else None
            if entries:
                last = entries[-1]
                if isinstance(last, dict) and str(last.get("version", "")).isdigit():
                    info["is_oa"] = True
                    add("https://www.%s.org/content/%sv%s.full.pdf" % (server, doi, last["version"]), server, 2)
                break
    # 8. Crossref: only pdf links that carry a Creative Commons style license
    cr = lookup("Crossref", "https://api.crossref.org/works/" + encoded + ("?mailto=" + quote(email, safe="@") if email else ""))
    if isinstance(cr, dict):
        info["ok"] = True
        message = cr.get("message") or {}
        open_license = [l.get("URL") for l in message.get("license") or [] if isinstance(l, dict) and "creativecommons.org" in str(l.get("URL", "")).lower()]
        if open_license:
            info["license"] = info["license"] or open_license[0]
            for link in message.get("link") or []:
                if isinstance(link, dict) and link.get("content-type") == "application/pdf":
                    info["is_oa"] = True
                    add(link.get("URL"), "Crossref", 0)

    seen, per_source, candidates = {}, {}, []
    for tier, order, url, source, version in sorted(found, key=lambda f: f[:2]):
        key = url_key(url)
        if key in seen:
            if version and not seen[key].get("version"):
                seen[key]["version"] = version
            continue
        if per_source.get(source, 0) >= MAX_PER_SOURCE:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        seen[key] = dict(url=url, source=source, version=version)
        candidates.append(seen[key])
    landing = list(dict.fromkeys(info["landing"]))
    if info["is_oa"] and "https://doi.org/" + doi not in landing:
        landing.append("https://doi.org/" + doi)
    return dict(candidates=candidates, is_oa=info["is_oa"], oa_status=info["oa_status"], license=info["license"],
                pmcid=info["pmcid"], attempts=attempts, lookup_ok=info["ok"], landing_urls=landing[:2],
                version=next((c["version"] for c in candidates if c.get("version")), None),
                manual_url=_web_url(info["manual_url"]) or (candidates[0]["url"] if candidates else None))


def _save(dest, body):
    with open(dest, "xb") as output:   # never overwrite
        output.write(body)


def download_oa(candidates, dest_path, deadline=None):
    """Try candidates in order (strings or {url, source}); stop at the first valid PDF.
    Returns dict(ok, source, attempts). dest_path is never overwritten."""
    dest = Path(dest_path)
    attempts = []
    challenge_url = None
    limited = set()   # hosts that answered 429: their remaining candidates are skipped, no retry
    if dest.exists():
        return dict(ok=False, source=None, challenge_url=None, attempts=[dict(kind="download", source="", host="", status="target exists", bytes=0, ok=False)])
    for candidate in candidates:
        url, source = (candidate, "") if isinstance(candidate, str) else (candidate.get("url", ""), candidate.get("source", ""))
        record = dict(kind="download", source=source, host=_host(url), status=None, bytes=0, ok=False)
        attempts.append(record)
        if _expired(deadline):
            record["status"] = "time cap"
            break
        if record["host"] in limited:
            record["status"] = "skipped (429)"
            continue
        try:
            status, body = _get(url, "application/pdf,*/*;q=0.5", MAX_BYTES, min(TIMEOUT, _left(deadline)), deadline, record)
            record.update(status=status, bytes=len(body))
            if not body.startswith(b"%PDF-"):
                record["status"] = "not a PDF (HTTP %s)" % status
                if challenge_in(body[:65536].decode("latin-1"), status if status in BLOCK_CODES else 200):
                    record["challenge"] = True
                    challenge_url = challenge_url or url
                continue
            _save(dest, body)
            record["ok"] = True
            return dict(ok=True, source=source, challenge_url=None, attempts=attempts,
                        version=None if isinstance(candidate, str) else candidate.get("version"))
        except OAError as exc:
            record["status"] = exc.status if str(exc).startswith("HTTP") else str(exc)[:60]
            if exc.status == 429:
                limited.add(record["host"])
            if exc.challenge:
                record["challenge"] = True
                challenge_url = challenge_url or url
        except FileExistsError:
            record["status"] = "target exists"
            break
        except OSError:
            record["status"] = "write error"
            break
    return dict(ok=False, source=None, challenge_url=challenge_url, attempts=attempts)


_WINDOW = threading.Lock()   # only one OA browser window at a time


def browser_download(url, dest_path, timeout=BROWSER_CAP, visible=False, profile_dir=None):
    """Fetch one OA pdf with Chromium in a dedicated profile (never a 科研通 profile), outside BrowserAgent.
    Headless: raises OAChallenge when a bot check shows up; the check is never solved or bypassed.
    Visible: waits for a person to complete the check, then captures the pdf download/response."""
    from playwright.sync_api import sync_playwright
    if not _WINDOW.acquire(blocking=False):
        raise OAError("another OA browser window is open")
    captured = []
    try:
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            profile = Path(profile_dir) if profile_dir else Path(tmp) / "profile"
            profile.mkdir(parents=True, exist_ok=True)
            options = dict(headless=not visible, accept_downloads=True, downloads_path=tmp)
            try:
                from runtime_paths import browser_options
                context = pw.chromium.launch_persistent_context(str(profile), **browser_options(), **options)
            except Exception:
                context = pw.chromium.launch_persistent_context(str(profile), **options)
            try:
                def on_download(download):
                    try:
                        path = Path(download.path())
                        if path.stat().st_size <= MAX_BYTES:
                            captured.append(path.read_bytes())
                    except Exception:
                        pass

                def on_response(response):
                    try:
                        if response.status == 200 and "application/pdf" in response.headers.get("content-type", "").lower():
                            body = response.body()
                            if body.startswith(b"%PDF-") and len(body) <= MAX_BYTES:
                                captured.append(body)
                    except Exception:
                        pass

                watch = lambda page: page.on("download", on_download)
                for page in context.pages:
                    watch(page)
                context.on("page", watch)
                context.on("response", on_response)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(url, timeout=min(timeout, 60) * 1000)
                except Exception as exc:
                    if not visible and "Download is starting" not in str(exc):
                        raise
                end = time.monotonic() + (timeout if visible else 8)
                while not captured and time.monotonic() < end:
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        break
                    if visible and not context.pages:
                        break
                if not captured and not visible:
                    try:
                        looks_blocked = challenge_in(page.title() + " " + page.content()[:200000])
                    except Exception:
                        looks_blocked = False
                    if looks_blocked:
                        raise OAChallenge()
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except OAError:
        raise
    except Exception as exc:
        raise OAError("browser: " + type(exc).__name__)
    finally:
        _WINDOW.release()
    if not captured or not captured[0].startswith(b"%PDF-"):
        raise OAError("browser: no PDF captured")
    _save(dest_path, captured[0])
    return len(captured[0])


def run_browser(browser, url, dest, attempts, timeout, visible, profile_dir):
    """Run one browser attempt and record it. Returns 'ok' | 'challenge' | 'failed'."""
    record = dict(kind="browser", source="Chromium" + (" (visible)" if visible else ""), host=_host(url), status=None, bytes=0, ok=False)
    attempts.append(record)
    try:
        record.update(bytes=browser(url, dest, timeout, visible=visible, profile_dir=profile_dir), status="ok", ok=True)
        return "ok"
    except OAChallenge:
        record.update(status="publisher check", challenge=True)
        return "challenge"
    except OAError as exc:
        record["status"] = str(exc)[:60]
    except Exception as exc:
        record["status"] = type(exc).__name__
    return "failed"


def lookup_only(doi, email="", core_api_key="", cache_root=None):
    """Find OA links without downloading anything (no account, no browser). status: found | not_oa | failed."""
    info = find_oa_locations(doi, email, time.monotonic() + TOTAL_CAP, core_api_key, cache_root)
    status = "found" if info["candidates"] else ("not_oa" if info["lookup_ok"] and not info["is_oa"] else "failed")
    return dict(status=status, oa_status=info["oa_status"], is_oa=info["is_oa"], license=info["license"], source=None,
                attempts=info["attempts"], challenge=False, manual_url=None, links=info["candidates"][:8], landing_url=info["manual_url"],
                version=info.get("version"), cached=bool(info.get("cached")), fetched_at=info.get("fetched_at"))


def fetch_open_access(doi, dest_path, email="", total_cap=TOTAL_CAP, browser=None, challenge_mode="prompt", profile_dir=None, candidates=None,
                      core_api_key="", cache_root=None):
    """Whole open-access attempt. Returns dict(status downloaded|not_oa|failed|needs_human, oa_status, is_oa, license,
    source, attempts, manual_url, challenge). challenge_mode: prompt (stop, ask the person) | popup (visible window) | skip."""
    browser = browser or browser_download
    deadline = time.monotonic() + total_cap
    if candidates is not None:   # the person chose a source we already found: no new lookup
        info = dict(candidates=list(candidates), is_oa=True, oa_status=None, license=None, attempts=[], lookup_ok=True,
                    manual_url=candidates[0]["url"] if candidates else None)
    else:
        info = find_oa_locations(doi, email, deadline, core_api_key, cache_root)
    got = download_oa(info["candidates"], dest_path, deadline)
    landing_attempts = []
    if not got["ok"] and info["is_oa"] and info["candidates"] is not None and not got.get("challenge_url") and not Path(dest_path).exists():
        # Before any browser: read the landing page(s) with a plain GET and try the PDF links they advertise.
        tried = {url_key(c["url"]) for c in info["candidates"]}
        limited = {a["host"] for a in got["attempts"] if a.get("status") in (429, "skipped (429)")}
        extra, landing_attempts = discover_landing(info, deadline, limited)
        extra = [c for c in extra if url_key(c["url"]) not in tried]
        if extra:
            info = dict(info, candidates=list(info["candidates"]) + extra)
            more = download_oa(extra, dest_path, deadline)
            got = dict(more, attempts=got["attempts"] + more["attempts"], challenge_url=got.get("challenge_url") or more.get("challenge_url"))
    attempts = info["attempts"] + landing_attempts + got["attempts"]
    source = got["source"]
    version = got.get("version") if got["ok"] else info.get("version")
    status, manual, challenge = None, info["manual_url"], False
    can_try = not got["ok"] and info["is_oa"] and info["candidates"] and not Path(dest_path).exists()
    if can_try:
        challenge_url = got.get("challenge_url")
        if not challenge_url and deadline - time.monotonic() > 10:
            failed = [a for a in got["attempts"] if not a["ok"]]
            # A 429 source is skipped for good: the headless browser must not become a retry of the same source.
            usable = [i for i, a in enumerate(failed) if a["status"] not in (429, "skipped (429)")]
            blocked = next((i for i in usable if failed[i]["status"] in BLOCK_CODES), usable[0] if usable else None)
            outcome = None
            if blocked is not None or not failed:
                target = info["candidates"][min(blocked or 0, len(info["candidates"]) - 1)]["url"]
                outcome = run_browser(browser, target, dest_path, attempts, min(BROWSER_CAP, deadline - time.monotonic()), False, profile_dir)
            if outcome == "ok":
                got, source = dict(ok=True), "Chromium"
            elif outcome == "challenge":
                challenge_url = target
        if not got["ok"] and challenge_url:
            challenge, manual = True, challenge_url
            if challenge_mode == "prompt":
                status = "needs_human"
            elif challenge_mode == "popup":
                if run_browser(browser, challenge_url, dest_path, attempts, HUMAN_CAP, True, profile_dir) == "ok":
                    got, source = dict(ok=True), "Chromium (visible)"
    if got["ok"]:
        status = "downloaded"
    elif status is None:
        status = "not_oa" if info["lookup_ok"] and not info["is_oa"] and not info["candidates"] else "failed"
    return dict(status=status, oa_status=info["oa_status"], is_oa=info["is_oa"], license=info["license"], source=source,
                version=version, attempts=attempts, challenge=challenge, links=info["candidates"][:8], landing_url=info["manual_url"],
                manual_url=manual if status in ("failed", "needs_human") and info["is_oa"] else None)


def human_fetch(url, dest_path, profile_dir=None, browser=None):
    """Button '打开验证窗口': visible window on the OA url; returns dict(status downloaded|needs_human, attempts, source)."""
    attempts = []
    outcome = run_browser(browser or browser_download, url, dest_path, attempts, HUMAN_CAP, True, profile_dir)
    return dict(status="downloaded" if outcome == "ok" else "needs_human", attempts=attempts, source="Chromium (visible)")
