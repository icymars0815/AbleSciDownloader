"""A single app workflow state, separate from PDF identity check results."""
from pipeline import stage_of


def workflow_state(item):
    if item.get("status") == "downloading": return "downloading"
    if item.get("status") == "rejected": return "rejected"
    if item.get("status") == "retrying": return "downloading"      # automatic retry pending: still in the download phase
    if stage_of(item) == "done": return "done"      # includes 已完结 on 科研通 and done_via oa
    if item.get("status") == "adopted": return "adopted"
    v = item.get("verification") or {}
    if item.get("status") in ("attention", "oa_review") or (v and v.get("status") != "identity_match"):
        return "review"
    if item.get("pdf"): return "downloaded"
    return "queued"
