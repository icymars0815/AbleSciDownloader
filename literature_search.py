"""Bounded, read-only OpenAlex work search; never starts a download or an assist request."""
import os
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode

from core import doi_value
from metadata import get_json

_semantic_lock = threading.Lock()
_last_semantic = 0.0

def search_works(query, cursor="*", year_from=None, year_to=None, open_access=False, page_size=50, mode="keyword", api_key=None):
    if mode not in ("keyword", "exact", "semantic"):
        raise ValueError("搜索模式须为 keyword、exact 或 semantic")
    limit = 2000 if mode == "semantic" else 500
    if not isinstance(query, str) or not query.strip() or len(query) > limit:
        raise ValueError("检索词为空或超过该模式的长度上限")
    if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
        raise ValueError("无效的翻页游标")
    filters = []
    for value, name in ((year_from, "from_publication_date"), (year_to, "to_publication_date")):
        if value not in (None, ""):
            if isinstance(value, bool) or not str(value).isdigit() or not 1800 <= int(value) <= 2100:
                raise ValueError("年份须为 1800 至 2100")
            if mode != "semantic":
                filters.append(name + ":" + str(value) + ("-01-01" if name.startswith("from") else "-12-31"))
    if year_from and year_to and int(year_from) > int(year_to):
        raise ValueError("起始年份不能晚于结束年份")
    if mode == "semantic":
        if year_from and year_to:
            filters.append("publication_year:%s-%s" % (year_from, year_to))
        elif year_from:
            filters.append("publication_year:>%d" % (int(year_from) - 1))
        elif year_to:
            filters.append("publication_year:<%d" % (int(year_to) + 1))
    if not isinstance(open_access, bool):
        raise ValueError("开放获取筛选参数无效")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ValueError("每页数量须为 1 至 100")
    if mode == "semantic" and (page_size > 50 or cursor != "*"):
        raise ValueError("语义搜索每次最多 50 条，不支持游标翻页")
    if open_access:
        filters.append("is_oa:true")
    search_field = {"keyword": "search", "exact": "search.exact", "semantic": "search.semantic"}[mode]
    params = {search_field: query.strip(), "per-page": page_size,
              "select": "id,doi,title,authorships,publication_year,primary_location,open_access,cited_by_count,is_retracted,relevance_score"}
    if mode != "semantic":
        params["cursor"] = cursor
    if filters:
        params["filter"] = ",".join(filters)
    key = (api_key if api_key is not None else os.environ.get("OPENALEX_API_KEY", "")).strip()
    if not key and mode != "semantic" and cursor != "*":
        raise ValueError("OpenAlex 游标翻页需要免费 API Key；请在服务进程环境中配置 OPENALEX_API_KEY 后重启")
    if key:
        params["api_key"] = key
    try:
        if mode == "semantic":
            global _last_semantic
            with _semantic_lock:
                delay = 1.0 - (time.monotonic() - _last_semantic)
                if delay > 0:
                    time.sleep(delay)
                _last_semantic = time.monotonic()
                data = get_json("https://api.openalex.org/works?" + urlencode(params))
        else:
            data = get_json("https://api.openalex.org/works?" + urlencode(params))
    except HTTPError as exc:
        if exc.code == 401:
            raise ValueError("OpenAlex 拒绝认证；检查 API Key，游标翻页需要免费密钥") from None
        if exc.code == 429:
            raise ValueError("OpenAlex 达到请求频率或额度限制，请稍后重试") from None
        raise ValueError("OpenAlex 检索失败（HTTP %s）" % exc.code) from None
    except Exception:
        raise ValueError("OpenAlex 检索失败，可能是网络、服务限额或接口不可用，请稍后重试") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("OpenAlex 返回格式异常")
    records = []
    for work in data["results"][:page_size]:
        if not isinstance(work, dict):
            continue
        location = work.get("primary_location") or {}
        source = location.get("source") or {}
        authors = [str((a.get("author") or {}).get("display_name") or "")[:200] for a in (work.get("authorships") or [])[:30] if isinstance(a, dict)]
        oa = work.get("open_access") or {}
        records.append(dict(id=str(work.get("id") or "")[:150], doi=doi_value(work.get("doi") or ""),
                            title=str(work.get("title") or "")[:2000], authors=authors,
                            year=work.get("publication_year"), journal=str(source.get("display_name") or "")[:300],
                            cited_by_count=work.get("cited_by_count"), is_oa=oa.get("is_oa"),
                            is_retracted=work.get("is_retracted"), relevance_score=work.get("relevance_score")))
    meta = data.get("meta") or {}
    return dict(records=records, total=meta.get("count"), next_cursor=meta.get("next_cursor") if mode != "semantic" else None,
                notice=str(meta.get("notice") or "")[:1000], cost_usd=meta.get("cost_usd"), mode=mode, source="OpenAlex",
                key_configured=bool(key), paging_requires_api_key=mode != "semantic",
                usage_note="匿名额度共享；关键词游标翻页需免费 API Key，可在设置中配置。" if not key else "使用已配置的 API Key；额度以 OpenAlex 当前政策为准。")
