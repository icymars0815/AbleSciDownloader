"""Rule-based rejection decisions. Missing metadata/OCR is not a wrong paper.
Official rules (docs/ABLESCI_RULES_FINDINGS.md): only four reasons are allowed, a category plus a concrete explanation is
mandatory, and the automatic path is limited to “the file is a different paper”, judged from the person's OWN DOI."""
from core import doi_value, site_url

REJECTION_RULES_VERSION = "2026-10-19-v1"

REJECT_CATEGORIES = (
    ("file_mismatch", "文件不一致", "上传的文件是另一篇文献（首页 DOI、标题、作者均不符）"),
    ("cannot_open", "文件无法打开（已多次尝试）", "更换软件、重新下载等多次尝试后仍无法打开"),
    ("missing_pages", "缺页（完整版页数更多）", "文件缺页，且已在出版社网站确认完整版页数更多"),
    ("forbidden_format", "禁止格式（caj、epub 等）", "文件为 caj、epub 等不允许的格式（含其压缩包）"),
)
CATEGORY_KEYS = tuple(c[0] for c in REJECT_CATEGORIES)
CATEGORY_LABELS = {c[0]: c[1] for c in REJECT_CATEGORIES}
NOT_ALLOWED = (
    "扫描件不够清晰",
    "格式不符（文件内容正确，仅版式或文件类型不满意）",
    "排版不同或为预印本（发布求助时科研通上已是正式版的除外）",
    "“已找到文件”“不再需要”",
)
PENALTIES = (
    "未按要求填写理由即强制采纳：扣除 2 倍积分",
    "以“不再需要”“已找到”为由驳回：扣除 2–5 倍积分",
    "多次驳回正确的文件：扣除 5 倍以上积分，并封号 7–30 天，严重者永久封号",
)
HUMAN_GUIDANCE = "扫描件不清晰、预印本、格式不符等情形由用户自行判断，软件不会自动驳回。科研通规则不允许以此类理由驳回，错误驳回会受到处罚。"
DETAIL_MIN, DETAIL_MAX = 4, 280


def reject_rules():
    """Shown in the reject dialog."""
    return dict(categories=[dict(key=k, label=l, hint=h) for k, l, h in REJECT_CATEGORIES],
                not_allowed=list(NOT_ALLOWED), penalties=list(PENALTIES), guidance=HUMAN_GUIDANCE,
                detail_min=DETAIL_MIN, detail_max=DETAIL_MAX)


def manual_reason(category, detail):
    """(category_key, text for the site). Raises ValueError (HTTP 400) unless one of the four categories and a concrete detail are given."""
    if category not in CATEGORY_KEYS:
        raise ValueError("请选择驳回理由类别（仅限科研通规定的 4 类）")
    detail = " ".join(str(detail or "").split())
    if len(detail) < DETAIL_MIN:
        raise ValueError("请填写具体说明（至少 %d 字），例如文件首页对应的文献" % DETAIL_MIN)
    if len(detail) > DETAIL_MAX:
        raise ValueError("具体说明请控制在 %d 字以内" % DETAIL_MAX)
    return category, CATEGORY_LABELS[category] + "：" + detail


def user_doi(item):
    """The DOI the person themselves supplied (import/paste/publish). A DOI that the site's smart extraction filled in
    (doi_origin == 'site', or a request synced from the site that never carried a DOI of ours) is NOT the person's information."""
    explicit = doi_value(item.get("user_doi") or "")
    if explicit:
        return explicit
    if item.get("doi_origin") == "site":
        return ""
    if item.get("doi_origin") == "user":
        return doi_value(item.get("expected_doi") or item.get("doi", ""))
    if item.get("expected_doi"):
        return doi_value(item["expected_doi"])
    ours = (item.get("publish_action") or item.get("request_confirmed") or item.get("import_format")
            or item.get("batch_id") or item.get("source") == "import")
    return doi_value(item.get("doi", "")) if ours else ""


def rejection_decision(item, preferences):
    v = item.get("verification") or {}
    anomaly = bool(v) and v.get("status") != "identity_match"
    target = user_doi(item)
    primary = v.get("primary_dois") or []
    definite = bool(target and primary and target not in primary and v.get("title_score", 100) < 65
                    and v.get("status") == "conflict")
    eligible = bool(item.get("source") == "website" and item.get("owned") and item.get("pdf")
                    and site_url(item.get("assist_url", ""), "/assist/detail")
                    and item.get("download_file_url") and v.get("sha256")
                    and not any(s in item.get("website_status", "") for s in ("完结", "关闭", "已采纳")))
    mode = preferences.get("rejection_mode", "manual")
    auto = (mode == "auto" and preferences.get("rejection_rules_version") == REJECTION_RULES_VERSION
            and definite and eligible and not item.get("reject_action"))
    if definite:
        reason = CATEGORY_LABELS["file_mismatch"] + "：文件首页 DOI（" + "、".join(primary[:3]) + "）与求助 DOI（" + target + "）不一致，标题也不符"
    else:
        reason = "信息异常或未核实，请人工判断。作者或摘要缺失、OCR 失败时不会自动驳回"
    return dict(mode=mode, anomaly=anomaly, definite_mismatch=definite, eligible=eligible,
                action="auto_reject" if auto else "ask_user" if anomaly else "none",
                category="file_mismatch" if definite else "", reason=reason)
