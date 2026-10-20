"""Pure rules for the 求助发布 flow (docs/PUBLISH_FLOW_DESIGN.md, docs/ABLESCI_RULES_FINDINGS.md).
No browser and no network here: the BrowserAgent scrapes the page and asks these functions what is allowed."""
import re
from difflib import SequenceMatcher

from core import doi_value, normalize

TITLE_MIN = 0.90          # lookup-layer title vs. the item's public (OpenAlex/Crossref) title
SIMILARITY_MIN = 50       # the site's own "相似度" shown in the lookup layer (percent)
JOURNAL_TYPE = "期刊论文"

# ---- 「最低悬赏被提高时」 setting ----
RAISE_MODES = ("ask", "auto")
RAISE_MAX_RANGE = (10, 500)
RAISE_MAX_DEFAULT = 50
RAISE_STEPS = (10, 20, 30, 50, 80, 100, 150, 200, 300, 500)
RESERVE_RANGE = (0, 5000)
RESERVE_DEFAULT = 0
RAISE_RULES_TEXT = "publish-raise-v1"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def raise_settings(prefs):
    """Effective setting. 'auto' only counts while the stored rules confirmation is present (damaged values fall back to ask)."""
    prefs = prefs or {}
    mode = prefs.get("publish_raise_mode")
    top = prefs.get("publish_raise_max")
    reserve = prefs.get("publish_reserve_points")
    out = dict(mode="ask",
               max=top if _is_int(top) and RAISE_MAX_RANGE[0] <= top <= RAISE_MAX_RANGE[1] else RAISE_MAX_DEFAULT,
               reserve=reserve if _is_int(reserve) and RESERVE_RANGE[0] <= reserve <= RESERVE_RANGE[1] else RESERVE_DEFAULT)
    if mode == "auto" and prefs.get("publish_raise_rules") == RAISE_RULES_TEXT:
        out["mode"] = "auto"
    return out


def validate_raise(body, current):
    """body: any of raise_mode / raise_max / reserve_points (+ raise_confirmed for turning auto on).
    Returns preference changes; raises ValueError (HTTP 400) for anything invalid."""
    changes = {}
    if "raise_mode" in body:
        if body["raise_mode"] not in RAISE_MODES:
            raise ValueError("请选择“询问”或“自动接受”")
        if body["raise_mode"] == "auto":
            if current["mode"] != "auto" and body.get("raise_confirmed") is not True:
                raise ValueError("请先阅读并确认“自动接受最低悬赏上调”的规则")
            changes.update(publish_raise_mode="auto", publish_raise_rules=RAISE_RULES_TEXT)
        else:
            changes.update(publish_raise_mode="ask", publish_raise_rules="")
    if "raise_max" in body:
        value = body["raise_max"]
        if not _is_int(value) or not RAISE_MAX_RANGE[0] <= value <= RAISE_MAX_RANGE[1]:
            raise ValueError("自动接受上限须为 %d–%d 的整数" % RAISE_MAX_RANGE)
        changes["publish_raise_max"] = value
    if "reserve_points" in body:
        value = body["reserve_points"]
        if not _is_int(value) or not RESERVE_RANGE[0] <= value <= RESERVE_RANGE[1]:
            raise ValueError("保留积分须为 %d–%d 的整数" % RESERVE_RANGE)
        changes["publish_reserve_points"] = value
    return changes


def raise_decision(needed, points, settings):
    """May the person's setting accept a minimum reward of `needed` points without asking? -> (bool, plain-Chinese reason)."""
    if settings["mode"] != "auto":
        return False, "当前设置为“询问”：该文献要求的最低悬赏为 %d 积分，需用户决定" % needed
    if needed > settings["max"]:
        return False, "该文献要求的最低悬赏为 %d 积分，超过自动接受上限 %d 积分，需用户决定" % (needed, settings["max"])
    if not _is_int(points):
        return False, "当前积分未知，无法核对保留积分；最低悬赏 %d 积分需用户决定" % needed
    if needed > points - settings["reserve"]:
        return False, "最低悬赏 %d 积分将使剩余积分低于保留值（现有 %d，保留 %d），需用户决定" % (needed, points, settings["reserve"])
    return True, "最低悬赏 %d 积分在自动接受范围内" % needed


# ---- reading the lookup layer ----
LABELS = dict(
    title=("标题", "题名", "篇名", "文献标题"),
    doi=("DOI", "doi"),
    journal=("期刊", "刊名", "来源", "出版物"),
    type=("文献类型", "类型"),
)


def _line_value(lines, labels):
    for n, line in enumerate(lines):
        for label in labels:
            m = re.match(r"^\s*" + re.escape(label) + r"\s*[:：\t ]\s*(.*)$", line)
            if m:
                value = m.group(1).strip()
                if not value:
                    value = next((x.strip() for x in lines[n + 1:n + 3] if x.strip()), "")
                return value
    return ""


def parse_lookup_text(text):
    """Fields shown in the 智能提取 result layer. The real wording is only partly known (docs list: 标题、相似度、DOI、
    期刊、作者、发表时间、类型、最低悬赏), so this is label-based and tolerant; anything not found stays empty/None."""
    text = str(text or "")
    lines = [x for x in text.splitlines() if x.strip()]
    sim = re.search(r"(?:相似度|匹配度)[^\d\n]{0,12}(\d+(?:\.\d+)?)\s*%?", text)
    low = re.search(r"最低(?:悬赏|积分|需要)[^\d\n]{0,15}(\d+)", text)
    found = dict(title=_line_value(lines, LABELS["title"]), doi=doi_value(_line_value(lines, LABELS["doi"]) or text),
                 journal=_line_value(lines, LABELS["journal"]), type=_line_value(lines, LABELS["type"]),
                 similarity=float(sim.group(1)) if sim else None, min_reward=int(low.group(1)) if low else None,
                 text=text.strip()[:1500])
    return found


CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
SUPPLEMENT = re.compile(r"supplement|supporting information|additional file|appendix|\bSI\b\s*(?:for|to)\b|补充材料|附录", re.I)
SUPPLEMENT_DOI = re.compile(r"[./-](?:s\d{3}|suppl?|sd\d+|supp-?\d+|mmc\d+)\b", re.I)


def title_similarity(a, b):
    a, b = normalize(a or ""), normalize(b or "")
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def auto_publish_block(titles, doi):
    """Why this item must never be auto-published (empty string = nothing against it). Non-English titles, supplementary
    material and (checked later against the layer) non-journal types are for the person to handle."""
    for title in titles:
        if CJK.search(title or ""):
            return "标题含中文、日文、韩文等非英文文字：此类文献不支持自动求助，请手动处理"
    for title in titles:
        if SUPPLEMENT.search(title or ""):
            return "疑似补充材料或附录：不自动求助，请自行判断"
    if SUPPLEMENT_DOI.search(doi or ""):
        return "DOI 形式疑似补充材料：不自动求助，请自行判断"
    return ""


def evaluate_lookup(user_doi, reference_title, scraped):
    """Compare the lookup layer with the item. Returns a list of plain-Chinese problems (empty = every check passed)."""
    problems = []
    if not scraped.get("doi") or scraped["doi"] != doi_value(user_doi):
        problems.append("弹窗中的 DOI（%s）与文献 DOI（%s）不一致" % (scraped.get("doi") or "未读取到", doi_value(user_doi)))
    if not reference_title:
        problems.append("未获取到该文献的公开标题，无法核对弹窗中的标题")
    else:
        score = title_similarity(scraped.get("title"), reference_title)
        if not scraped.get("title") or score < TITLE_MIN:
            problems.append("弹窗中的标题与公开记录相似度仅 %d%%（需 ≥ %d%%）" % (round(score * 100), TITLE_MIN * 100))
    # An exact DOI lookup shows no 相似度 / 类型 / 最低悬赏 in the popup (real page, 2026-10-20); those are
    # then checked on the filled form (type radio, #point-low). When the popup does show them, they must pass.
    sim = scraped.get("similarity")
    if sim is not None and sim < SIMILARITY_MIN:
        problems.append("科研通给出的相似度仅 %g%%，低于 %d%%" % (sim, SIMILARITY_MIN))
    if scraped.get("type") and JOURNAL_TYPE not in str(scraped["type"]):
        problems.append("文献类型为“%s”，非期刊论文：书籍、章节、学位论文等不自动求助" % scraped["type"])
    return problems


# ---- failures ----
FAILURES = (
    ("email_unverified", re.compile(r"邮箱.{0,12}(未验证|未激活|验证后)|(请|需).{0,6}验证.{0,6}邮箱|验证邮箱"),
     "科研通提示邮箱尚未验证。请先在科研通验证邮箱，再求助", False),
    ("rules_required", re.compile(r"(先|需|请).{0,8}学习.{0,10}(规则|须知)|求助须知.{0,12}(学习|阅读|考试|答题)|通过.{0,6}(规则|须知).{0,6}(考试|测试)"),
     "科研通要求先学习求助规则（须知）。请在科研通完成学习后再求助", False),
    ("gate", re.compile(r"(先|需|请|须).{0,8}(处理|采纳|驳回|确认).{0,12}(应助|求助)|(未|尚未|没有).{0,4}(处理|采纳|驳回).{0,10}(应助|求助)|(应助|求助).{0,10}(未处理|待处理|尚未处理)|已有.{0,6}应助.{0,10}(处理|采纳)"),
     "科研通提示有已收到的应助需先处理（采纳或驳回），已暂停发布，处理后再继续", False),
    ("restricted", re.compile(r"受限"),
     "科研通提示账号受限，已暂停自动流程。请在科研通查看原因，处理后再开启", True),
    ("daily_limit", re.compile(r"(每日|每天|今日|今天).{0,10}(发布|求助).{0,10}(上限|已达|最多|限)|(发布|求助).{0,8}(次数|数量).{0,8}(上限|已达|超)"),
     "科研通提示今日求助次数已达上限，已暂停自动流程，明日再继续", True),
    ("state_not_allowed", re.compile(r"状态.{0,8}(不允许|不能|无法|不支持)|(不允许|不能|无法).{0,8}状态"),
     "科研通提示当前求助状态不允许该操作，请在科研通查看", False),
)


def classify_failure(text):
    """Known failure wording -> dict(kind, message, pause), else None. All of them mean: stop, no retry."""
    text = str(text or "")
    for kind, pattern, message, pause in FAILURES:
        if pattern.search(text):
            return dict(kind=kind, message=message, pause=pause)
    return None


PAGE_RESTRICTED = re.compile(r"(账号|账户|您的|你的)[^。\n]{0,12}受限|受限[^。\n]{0,6}(账号|账户)")


def page_restricted(body_text):
    """Restricted-account notice in the page body (narrower than a bare “受限”, which also appears in help text)."""
    return bool(PAGE_RESTRICTED.search(str(body_text or "")))


def min_reward_jump(new_min, seen):
    """Minimum reward suddenly 2x of what this account saw recently and >= 50 -> likely a restricted account."""
    seen = [v for v in (seen or []) if _is_int(v) and v > 0]
    return bool(_is_int(new_min) and new_min >= 50 and seen and new_min >= 2 * min(seen[-5:]))


# ---- amount / raise text in a code-2 「确认提示」 ----
RAISE_RE = re.compile(r"(?:最低|至少|不低于|不得低于)[^\d\n，。,.]{0,10}(?:悬赏|积分)[^\d\n]{0,12}(\d+)|(?:悬赏|积分)[^\d\n]{0,8}(?:将|会|自动)?[^\d\n]{0,6}(?:提高|调整|增加|提升)[^\d\n]{0,10}(\d+)")


OTHER_CODE2 = re.compile(r"重复|已存在|已经求助|已有.{0,6}求助|冲突|不一致|疑似|相同|已求助")


def parse_raise(text):
    """code-2 confirmation text -> required points when (and only when) it is a “最低悬赏被提高” notice, else None."""
    text = str(text or "")
    if OTHER_CODE2.search(text):
        return None     # duplicate request / conflicting information etc.: never treated as a plain reward raise
    m = RAISE_RE.search(text)
    if not m:
        return None
    return int(m.group(1) or m.group(2))
