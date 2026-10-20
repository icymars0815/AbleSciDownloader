"""Site-file deletion clock: 科研通 deletes the file 24 h after a request is completed.
Completion time comes from the item (completed_at / completion_time / finish_time / finished_at) when known; otherwise the
first time this app saw it completed, which is labelled an estimate."""
from datetime import datetime, timedelta

from pipeline import stage_of, FULFILLED

DELETE_AFTER = timedelta(hours=24)
WARN_BEFORE = timedelta(hours=2)
TIME_FIELDS = ("completed_at", "completion_time", "finish_time", "finished_at")
SITE_DONE = ("已完结", "已采纳")


def _parse(value):
    try:
        dt = datetime.fromisoformat(str(value).strip()) if value else None
    except ValueError:
        return None
    return dt.astimezone() if dt and dt.tzinfo is None else dt


def is_completed(item):
    return (item.get("source") == "website" and ((item.get("website_status") or "").strip() in SITE_DONE
            or (item.get("adopt_action") or {}).get("status") == "confirmed" or item.get("status") == "adopted"))


def completion(item):
    """(datetime, estimated) or None."""
    for key in TIME_FIELDS:
        dt = _parse(item.get(key))
        if dt:
            return dt, False
    dt = _parse(item.get("completed_seen_at"))
    return (dt, True) if dt else None


def fmt(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def view(item):
    """Per-item deletion info for the UI: state none|pending|scheduled|deleted."""
    if item.get("source") != "website":
        return dict(state="none")
    local = bool(item.get("pdf"))
    if item.get("site_file_deleted"):
        return dict(state="deleted", local_copy=local, text="科研通文件已删除" + ("，本地副本保留" if local else ""))
    if is_completed(item):
        found = completion(item)
        if found:
            at = found[0] + DELETE_AFTER
            return dict(state="scheduled", at=at.isoformat(), estimated=found[1], local_copy=local,
                        text="预计删除时间：" + fmt(at) + ("（估算）" if found[1] else ""))
        return dict(state="scheduled", local_copy=local, text="预计删除时间：计算中")
    if stage_of(item) in ("fulfilled", "downloading", "awaiting_decision") or (item.get("website_status") or "").strip() in FULFILLED:
        return dict(state="pending", local_copy=local, text="完结后 24 小时删除")
    return dict(state="none")


def sweep(store, notifier=None, now=None):
    """Stamp first-seen completion, set site_file_deleted when the clock ran out, warn 2 h ahead if not downloaded."""
    now = now or datetime.now().astimezone()
    changed = 0
    for item in store.snapshot()["items"]:
        if item.get("source") != "website" or item.get("site_file_deleted") or not is_completed(item):
            continue
        found = completion(item)
        if not found:
            store.update(item["id"], completed_seen_at=now.isoformat(timespec="seconds"))
            found = (now, True)
            changed += 1
        at = found[0] + DELETE_AFTER
        title = (item.get("title") or item.get("doi") or "?")[:40]
        if now >= at:
            store.update(item["id"], site_file_deleted=True, site_file_deleted_at=at.isoformat(timespec="seconds"))
            store.event("《%s》的科研通文件已到删除时间。" % title + ("本地副本保留。" if item.get("pdf") else "尚未下载，已无法从科研通获取。"),
                        kind="deletion", item_id=item["id"], action="focus")
            changed += 1
        elif now >= at - WARN_BEFORE and not item.get("pdf") and notifier:
            notifier.notify("needs_action", item["id"] + ":delete_soon", "科研通文件即将删除",
                            "《%s》的科研通文件预计 %s 删除，尚未下载，请尽快下载。" % (title, fmt(at)))
    return changed


def seconds_left(item, now=None):
    """Seconds until the nearest known deadline (site-file deletion, or fulfilment time + auto_adopt_hours); None if unknown."""
    now = now or datetime.now().astimezone()
    left = []
    at = _parse(view(item).get("at"))
    if at:
        left.append((at - now).total_seconds())
    hours = item.get("auto_adopt_hours")
    base = _parse(item.get("fulfilled_at") or item.get("website_status_time"))
    if isinstance(hours, int) and not isinstance(hours, bool) and base:
        left.append((base + timedelta(hours=hours) - now).total_seconds())
    return min(left) if left else None
