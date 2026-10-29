"""Intake review: import batches, free-source screening bookkeeping, the confirmation table and reward validation.
Nothing here talks to 科研通; confirming only marks items (request_confirmed) so the pipeline may later act on them."""
import uuid
from datetime import datetime

from core import now, is_retracted
from pipeline import stage_of
import publish_policy as pp

REWARD_STEPS = (10, 20, 30, 50, 80, 100, 150, 200)
REWARD_MIN = 10
REWARD_DEFAULT = 10
UNKNOWN_POINTS_CAP = 200
KEEP_BATCHES = 30


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def reward_cap(points):
    return points if _is_int(points) else UNKNOWN_POINTS_CAP


def reward_tiers(points):
    """Official reward tiers the current balance can pay for."""
    cap = reward_cap(points)
    return [v for v in REWARD_STEPS if v <= cap]


def validate_reward(value, points):
    """Reward points: only the official tiers, and not above the current points (200 cap when the balance is unknown)."""
    if not _is_int(value):
        raise ValueError("悬赏积分须为整数")
    cap = reward_cap(points)
    if cap < REWARD_MIN:
        raise ValueError("当前积分不足 %d，无法设置悬赏" % REWARD_MIN)
    if value not in REWARD_STEPS:
        raise ValueError("悬赏积分仅可选 %s" % "、".join(str(v) for v in REWARD_STEPS))
    if value > cap:
        raise ValueError("悬赏积分不能超过当前积分 %d" % cap)
    return value


def default_reward(prefs, points=None):
    value = (prefs or {}).get("default_reward")
    value = value if _is_int(value) and value >= REWARD_MIN else REWARD_DEFAULT
    tiers = reward_tiers(points)
    if not tiers:
        return value
    return max([v for v in tiers if v <= value] or [tiers[0]])


# ---------- batches ----------
def new_batch_id():
    return uuid.uuid4().hex[:10]


def create_batch(store, batch_id, account_id, item_ids, origin="app"):
    with store.lock:
        batch = dict(id=batch_id, account_id=account_id, origin=origin, created=now(), item_ids=list(item_ids), screened=[], screening_done_at=None,
                     confirmed_at=None, confirmed_ids=[], prompted=False, deferred_at=None)
        batches = store.data.setdefault("batches", [])
        batches.append(batch)
        store.data["batches"] = batches[-KEEP_BATCHES:]
        store.save()
    store.event("已导入 %d 条新文献，开始筛选免费全文。" % len(item_ids), kind="intake", batch_id=batch_id)
    return batch


def find_batch(store, batch_id):
    return next((b for b in store.data.get("batches", []) if b["id"] == batch_id), None)


def _items_of(snapshot, batch):
    by_id = {i["id"]: i for i in snapshot["items"]}
    return [by_id[i] for i in batch["item_ids"] if i in by_id]


def screening_candidate(item):
    return (item.get("source") == "import" and bool(item.get("doi")) and not item.get("oa_check") and not item.get("pdf")
            and stage_of(item) == "queued")


def next_screening(store, oa_first=True, notifier=None):
    """Item id to screen next (marked as screened immediately), or None; a batch with nothing left is finished."""
    finished = []
    with store.lock:
        for batch in store.data.get("batches", []):
            if batch.get("screening_done_at"):
                continue
            if oa_first:
                for item in _items_of(store.data, batch):
                    if screening_candidate(item):
                        # An interrupted worker may have marked the item before saving its result.
                        # Pipeline.screen_step serializes workers, so a still-missing result is retryable.
                        if item["id"] not in batch["screened"]:
                            batch["screened"].append(item["id"])
                            store.save()
                        return item["id"]
            batch["screening_done_at"] = now()
            finished.append(batch["id"])
        if finished:
            store.save()
    for bid in finished:
        _announce_screened(store, bid, notifier)
    return None


def _announce_screened(store, batch_id, notifier=None):
    snap = store.snapshot()
    batch = next(b for b in snap["batches"] if b["id"] == batch_id)
    rows = [classify(i, True) for i in _items_of(snap, batch)]
    wanted = sum(1 for r in rows if r["checked"])
    review_count = sum(1 for r in rows if r["code"] == "oa_review")
    if review_count and not any(r["selectable"] for r in rows):
        store.event("免费全文筛选完成：%d 篇已下载，待核对。" % review_count, kind="intake", batch_id=batch_id, action="open-batch")
    elif any(r["selectable"] for r in rows):
        store.event("免费全文筛选完成：%d 条建议向科研通求助，待确认。" % wanted, kind="intake", batch_id=batch_id, action="open-batch")
        if notifier and batch.get("origin") != "mcp":
            notifier.notify("needs_action", "batch:" + batch_id, "待确认求助清单", "免费全文筛选完成：%d 条建议向科研通求助，待确认。" % wanted)
    else:
        store.event("免费全文筛选完成：无需向科研通求助。", kind="intake", batch_id=batch_id)


def classify(item, screened_done=True):
    """One confirmation-table row: free-source result, advice and whether it is selectable / pre-checked."""
    stage = stage_of(item)
    st = (item.get("oa_check") or {}).get("status")
    oa = item.get("oa_check") or {}
    retracted = is_retracted(item.get("metadata_ref") or {}) is True or ((item.get("verification") or {}).get("retraction") or {}).get("retracted") is True
    row = dict(id=item["id"], title=item.get("title", ""), doi=item.get("doi", ""), code="other", result="", advice="", checked=False, selectable=False,
               version=oa.get("version") or "", retracted=bool(retracted))

    def done(code, result, advice, checked=False, selectable=False):
        row.update(code=code, result=result, advice=advice, checked=checked, selectable=selectable)
        return row
    if item.get("request_confirmed") is True:
        return done("confirmed", "已确认向科研通求助", "已在求助中")
    if stage == "done" and item.get("done_via") == "oa":
        return done("oa_done", "免费全文已下载，核对通过", "无需求助")
    if stage == "oa_review":
        return done("oa_review", "免费全文已下载，核对未完全通过", "请在任务列表中确认，或改走科研通")
    if not item.get("doi"):
        return done("no_doi", "无 DOI，无法自动筛选", "请补充 DOI 或手动求助")
    if stage != "queued":
        return done("other", "已在求助中", "无需再求助")
    if st is None:
        if not screened_done:
            return done("pending", "等待筛选", "")
        return done("unscreened", "未做免费全文筛选", "向科研通求助", True, True)
    if st == "not_oa":
        return done("not_oa", "暂未找到免费全文", "可继续向科研通求助", True, True)
    if st == "failed":
        return done("oa_failed", "免费全文暂未取得", "可继续向科研通求助", True, True)
    if st == "skipped":
        return done("skipped", "已改走科研通", "向科研通求助", True, True)
    if st == "needs_human":
        return done("needs_human", "需人工验证", "请先完成验证，或改走科研通", False, True)
    if st == "found":
        return done("found", "找到免费全文链接，尚未下载", "可先尝试免费链接", False, True)
    return done("other", "免费全文状态：" + str(st), "", False, True)


def phase_of(batch, items):
    if not batch.get("screening_done_at"):
        return "screening"
    mine = [i for i in items if i.get("request_batch_id") == batch["id"] and i.get("request_confirmed") is True]
    if not mine:
        return "awaiting_confirmation" if any(classify(i)["selectable"] or classify(i)["code"] == "oa_review" for i in items) else "done"
    stages = [stage_of(i) for i in mine]
    if any(s in ("queued", "attention") for s in stages):
        return "requesting"
    if any(s in ("submitted", "fulfilled", "downloading", "awaiting_decision") for s in stages):
        return "monitoring"
    if any(classify(i)["code"] == "oa_review" for i in items):
        return "awaiting_confirmation"
    return "done"


def _seconds(a, b):
    try:
        return max(0, int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()))
    except (TypeError, ValueError):
        return None


def public_batch(batch, items, prefs, points):
    phase = phase_of(batch, items)
    done_screen = bool(batch.get("screening_done_at"))
    rows = []
    for i in items:
        r = classify(i, done_screen)
        r["reward"] = i.get("request_reward") or default_reward(prefs, points)
        rows.append(r)
    mine = [i for i in items if i.get("request_batch_id") == batch["id"] and i.get("request_confirmed") is True]
    stages = [stage_of(i) for i in mine]
    total = len(items)
    with_doi = sum(1 for i in items if i.get("doi"))
    screened = sum(1 for i in items if i.get("doi") and (i.get("oa_check") or i.get("pdf") or i["id"] in batch.get("screened", [])))
    want = len(mine) if mine else sum(1 for r in rows if r["checked"])
    published = sum(1 for s in stages if s in ("submitted", "fulfilled", "downloading", "awaiting_decision", "done"))
    got = sum(1 for s in stages if s in ("fulfilled", "downloading", "awaiting_decision", "done"))
    finished = sum(1 for s in stages if s == "done") + sum(1 for r in rows if r["code"] == "oa_done")
    errors = sum(1 for s in stages if s == "attention")
    spent = _seconds(batch["created"], batch.get("screening_done_at"))
    steps = [dict(key="import", label="导入 %d 条" % total, status="done", detail="")]
    steps.append(dict(key="screen", label="免费全文筛选 %d/%d" % (screened, with_doi), status="done" if done_screen else "active",
                      detail=("用时 %d 分 %d 秒" % divmod(spent, 60)) if done_screen and spent is not None else ""))
    if phase == "screening":
        conf = "pending"
    elif phase == "awaiting_confirmation":
        conf = "waiting_you"
    elif batch.get("confirmed_at"):
        conf = "done"
    else:
        conf = "skipped"
    review_count = sum(1 for r in rows if r["code"] == "oa_review")
    label = ("已确认 %d 条求助" % len(mine) if conf == "done" else
             "免费全文待核对 %d 条" % review_count if review_count and not want else "待确认（%d 条需求助）" % want)
    steps.append(dict(key="confirm", label=label, status=conf, detail=""))
    confirmed = conf == "done"
    req = "error" if errors else "pending" if not confirmed else "done" if phase in ("monitoring", "done") else "active"
    steps.append(dict(key="request", label="科研通求助 %d/%d" % (published, len(mine)), status=req, detail=""))
    steps.append(dict(key="wait", label="等待应助", status="pending" if not confirmed else "active" if "submitted" in stages else "done" if published else "pending", detail=""))
    steps.append(dict(key="download", label="下载与核对 %d/%d" % (got, len(mine)),
                      status="pending" if not confirmed else "done" if mine and got == len(mine) else "active" if got else "pending", detail=""))
    steps.append(dict(key="done", label="完成 %d/%d" % (finished, total), status="done" if phase == "done" and done_screen else "pending", detail=""))
    return dict(id=batch["id"], created=batch["created"], phase=phase, prompted=bool(batch.get("prompted")), deferred=bool(batch.get("deferred_at")),
                total=total, counts=dict(total=total, with_doi=with_doi, screened=screened, to_request=want, confirmed=len(mine),
                                         published=published, got=got, finished=finished, errors=errors),
                steps=steps, items=rows)


def public_batches(snapshot, account_id, prefs, points):
    out = []
    for batch in snapshot.get("batches", []):
        if batch.get("account_id", "default") == account_id:
            out.append(public_batch(batch, _items_of(snapshot, batch), prefs, points))
    return list(reversed(out))[:10]


def prepare_confirm(store, batch_id, item_ids, rewards, points, account_id):
    """Validate a confirmation without changing anything. Returns [(item, reward)] in request order (item ids default to the pre-checked rows)."""
    rewards = rewards if isinstance(rewards, dict) else {}
    with store.lock:
        batch = find_batch(store, batch_id)
        if not batch or batch.get("account_id", "default") != account_id:
            raise ValueError("未找到该导入记录")
        if not batch.get("screening_done_at"):
            raise ValueError("免费全文筛选尚未完成，请稍后再试")
        prefs = store.data.get("preferences", {})
        items = {i["id"]: i for i in store.data["items"] if i["id"] in batch["item_ids"]}
        if item_ids is None:
            item_ids = [i for i in batch["item_ids"] if i in items and classify(items[i])["checked"]]
        if not isinstance(item_ids, list) or not item_ids or any(not isinstance(i, str) for i in item_ids):
            raise ValueError("没有可求助的条目")
        chosen = []
        for iid in dict.fromkeys(item_ids):
            item = items.get(iid)
            if not item:
                raise ValueError("所选条目不在该导入记录中")
            if not classify(item)["selectable"]:
                raise ValueError("《%s》当前不能求助（无 DOI、已有免费全文或已在求助中）" % (item.get("title") or "?")[:30])
            chosen.append((dict(item), validate_reward(rewards.get(iid, item.get("request_reward") or default_reward(prefs, points)), points)))
        return chosen


def confirm(store, batch_id, item_ids, rewards, points, session, account_id):
    """Mark the chosen items as confirmed for 科研通 requests. Raises ValueError with a user-readable reason."""
    if not session:
        raise ValueError("请先登录科研通再求助")
    if not isinstance(item_ids, list) or not item_ids or any(not isinstance(i, str) for i in item_ids):
        raise ValueError("请至少选择一条")
    rewards = rewards if isinstance(rewards, dict) else {}
    with store.lock:
        batch = find_batch(store, batch_id)
        if not batch or batch.get("account_id", "default") != account_id:
            raise ValueError("未找到该导入记录")
        if not batch.get("screening_done_at"):
            raise ValueError("免费全文筛选尚未完成，请稍后再试")
        prefs = store.data.get("preferences", {})
        items = {i["id"]: i for i in store.data["items"] if i["id"] in batch["item_ids"]}
        chosen = []
        for iid in dict.fromkeys(item_ids):
            item = items.get(iid)
            if not item:
                raise ValueError("所选条目不在该导入记录中")
            if not classify(item)["selectable"]:
                raise ValueError("《%s》当前不能求助（无 DOI、已有免费全文或已在求助中）" % (item.get("title") or "?")[:30])
            chosen.append((iid, validate_reward(rewards.get(iid, item.get("request_reward") or default_reward(prefs, points)), points)))
        if isinstance(points, int) and not isinstance(points, bool):
            reserve = pp.raise_settings(prefs)["reserve"]
            total = sum(r for _, r in chosen)
            if total > points - reserve:
                raise ValueError("本次悬赏合计 %d 积分，超出可用额度 %d（现有 %d，保留 %d）。请减少条目或降低悬赏"
                                 % (total, max(points - reserve, 0), points, reserve))
        stamp = now()
        for iid, reward in chosen:
            items[iid].update(request_confirmed=True, request_confirmed_at=stamp, request_batch_id=batch_id, request_reward=reward, updated=stamp)
        batch["confirmed_at"] = stamp
        batch["confirmed_ids"] = sorted(set(batch.get("confirmed_ids", [])) | {i for i, _ in chosen})
        batch["prompted"] = True
        store.save()
    store.event("已确认 %d 条向科研通求助，仅这些条目会被发布。" % len(chosen), kind="intake", batch_id=batch_id)
    return len(chosen)


def mark(store, batch_id, account_id, **fields):
    with store.lock:
        batch = find_batch(store, batch_id)
        if not batch or batch.get("account_id", "default") != account_id:
            raise ValueError("未找到该导入记录")
        batch.update(fields)
        store.save()
