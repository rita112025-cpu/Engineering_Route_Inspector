"""Deterministic reading of one specification sentence: vocabulary, numbers with units, comparison direction.

No model, no network, no learning: a fixed list of sentence shapes, tried in a fixed order. A shape that does not
fit exactly yields a *problem* (the candidate then waits for a person) and never a guess:

* the comparison direction comes only from the words (``不得小於`` = ``>=``); none found -> NO_DIRECTION
* a number without a unit is never taken as millimetres
* ``宜 / 建議 / 儘量`` are advisory, not mandatory
* "appropriate / convenient / tidy" wording without a number is NOT_ENFORCEABLE (a 2D drawing cannot decide it)

Only the rule types the analyzer can already run (``app.templates.TEMPLATES``) are produced.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from core.evidence.chunks import UNIT_WORDS
from .candidate import ADVISORY, MANDATORY, UNKNOWN_MODALITY, fmt_num

MAX_SENTENCE = 600
UNIT_TO_MM = {"mm": Decimal(1), "cm": Decimal(10), "m": Decimal(1000), "ft": Decimal("304.8"), "in": Decimal("25.4")}
ZH_DIGITS = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# ---- direction words (matched on NFKC text) -------------------------------------------------------------------------
GE_WORDS = ["不應小於", "不應少於", "不應低於", "不宜小於", "不宜少於", "不宜低於", "不可小於", "不可少於", "不能小於", "不能少於",
            "不得小於", "不得少於", "不得低於", "不得短於", "不小於", "不少於", "不低於", "不短於", "大於或等於", "大於等於",
            "至少", "最小", "最低", "最短", ">=", "≥", "not less than", "no less than", "at least", "minimum", "min."]
LE_WORDS = ["不應大於", "不應超過", "不應多於", "不應高於", "不宜大於", "不宜超過", "不宜高於", "不可大於", "不可超過", "不能大於",
            "不能超過", "不得大於", "不得超過", "不得多於", "不得高於", "不得長於", "不得逾", "不大於", "不多於", "不超過", "不高於",
            "不長於", "小於或等於", "小於等於", "至多", "最多", "最大", "最高", "最長", "<=", "≤", "not more than",
            "no more than", "not exceed", "shall not exceed", "at most", "maximum", "max."]
BARE_DIRECTION = re.compile(r"超過|大於|小於|少於|低於|高於|多於")
_DIR_RE = re.compile("|".join(re.escape(w) for w in sorted(GE_WORDS + LE_WORDS, key=len, reverse=True)), re.I)
_POSTFIX_RE = None   # built below, needs the quantity pattern

# ---- modality -------------------------------------------------------------------------------------------------------
SOFTENERS = re.compile(r"建議|宜(?!用)|儘量|盡量|儘可能|盡可能|最好|適宜|should|recommend|preferabl|if possible", re.I)
MANDATORY_RE = re.compile(r"不得|不可|不應|禁止|嚴禁|必須|必需|應(?![用力變])|須|需(?=要|於|設|保|留|有)|至少|shall|must", re.I)
ADVISORY_RE = re.compile(r"宜|可(?!能)|\bmay\b", re.I)
# wording that no drawing can decide
VAGUE_RE = re.compile(r"適當|適度|適宜|足夠|充足|充分|方便|便於|易於|整齊|美觀|儘量|盡量|儘可能|盡可能|避免影響|不影響|妥善|"
                      r"合理|縮短|最短路徑|良好|必要時|視需要|視情況|依需要|appropriate|adequate|sufficient|convenient|"
                      r"as far as practicable", re.I)
# a sentence is worth keeping as "cannot be checked automatically" only when it is about routing / layout
ROUTE_RE = re.compile(r"管線|電纜|線纜|纜線|光纜|線槽|線架|管架|橋架|支架|拉線箱|接線箱|導管|配管|風管|水管|管道|管路|機房|間距|淨距|"
                      r"距離|間隔|淨空|交叉|穿越|路徑|走向|敷設|佈設|布設|維修|檢修|操作空間|"
                      r"pipe|cable|duct|tray|conduit|clearance|routing", re.I)
# prose about documents and evidence handling, not about the physical layout ("不得逕自認定", "須回查 Source ...")
DOC_PROSE_RE = re.compile(r"逕自|逾越|引用|認定|等同|證據|文件|版本|Findings?|Source|Evidence|檔案|欄位", re.I)
LAYOUT_VERB_RE = re.compile(r"保持|維持|預留|留設|設置|設於|設在|佈設|布設|敷設|配置|穿越|通過|跨越|距離|間距|淨距|間隔|淨空|"
                            r"交叉|固定|支撐|隔離|分離|檢修|維修|安裝|裝設|排列|走向|路徑")
_MD_LEAD = re.compile(r"^\s*(?:[-*+>]\s+|#{1,6}\s+|\|+\s*)+")

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT_ALT = "|".join(re.escape(w) for w in sorted(UNIT_WORDS, key=len, reverse=True))
QTY_RE = re.compile(rf"(?<![\d.])({_NUM})\s*(?:({_UNIT_ALT})(?![A-Za-z0-9²³/^·×]))?", re.I)
COUNT_RE = re.compile(r"(?<![\d.])(\d+|[一二兩三四五六七八九十]+)\s*(個|處|組|支|條|只|台|根|座|點|套|盞)")
_POSTFIX_RE = re.compile(rf"(?<![\d.])({_NUM})\s*(?:({_UNIT_ALT})(?![A-Za-z0-9²³/^·×]))?\s*(以上|以下|以內|以外)", re.I)

# ---- measurement shapes ---------------------------------------------------------------------------------------------
_AXIS_KW = r"(淨距|間距|距離|淨空|間隔|間隙|淨高)"
H_CLEAR = re.compile(r"(?:水平|橫向|平面)\s*(?:方向)?(?:之|的)?" + _AXIS_KW)
V_CLEAR = re.compile(r"(?:垂直|豎向|垂向|高程差|上下)\s*(?:方向)?(?:之|的)?(?:" + _AXIS_KW + ")?")
GENERIC_DIST = re.compile(_AXIS_KW)
SPACING_VERB = re.compile(r"保持|維持|預留|留設|保留|間隔|相距|淨空")
NO_CROSS_A = re.compile(r"(.+?)(?:不得|不可|不應|禁止|嚴禁)(?:與|和|跟)(.+?)(?:相)?(?:交叉|交錯|相交)")
NO_CROSS_B = re.compile(r"(.+?)(?:不得|不可|不應|禁止|嚴禁)(?:相)?(?:交叉|交錯|相交)")
NO_CROSS_C = re.compile(r"(?:禁止|嚴禁)(.+?)(?:與|和|跟)(.+?)(?:相)?(?:交叉|交錯|相交)")
KEEP_OUT = re.compile(r"(.*?)(?:不得|不可|不應|禁止|嚴禁)(?:進入|侵入|穿越|穿入|佔用|占用)(.+)")
INSIDE_ZONE = re.compile(r"(.+?)(?:必須|應|須|需)(?:完全)?(?:位於|設置於|設於|布設於|佈設於|敷設於|限於)(.+?)(?:之?內|範圍內|區域內)")
LENGTH_KW = re.compile(r"長度")
ORTHO_KW = re.compile(r"水平或垂直|垂直或水平|平行或垂直|垂直或平行|正交|橫平豎直")
CONNECTORS = re.compile(r"與|和|及|跟|距離?|至(?!少)")
_LEAD = re.compile(r"^(?:另外|此外|同時|且|並|而|但|惟|其|凡是|凡|所有|各類|各)+")
_TRAIL = re.compile(r"(?:兩者|雙方|二者)?(?:之間|間)?(?:之|的)?(?:應|須|必須|需)?(?:保持|維持|預留|留設|保留|具有|有)?"
                    r"(?:之|的)?(?:最小|最低|最大)?$")
_PART_LEAD = re.compile(r"^(?:最近的|最近之|最近|該|其|各|所有)+")
_NUMBERING = re.compile(r"^\s*(?:[(（]?[0-9一二三四五六七八九十]+[.)、）]\s*|\d+(?:\.\d+)+\s+|第[一二三四五六七八九十百\d]+[章節條款項]\s*|"
                        r"[A-Z]\.\d+(?:\.\d+)*\s+|[A-Za-z][.．、)）]\s+|附錄\s*[A-Z](?:-\d+)?\s*|[附錄]+\s*)+")


@dataclass
class Parsed:
    template_id: str | None
    measurement: str | None
    operator: str | None
    value: float | None
    unit: str | None
    value_text: str
    subject_text: str = ""
    target_text: str = ""
    zone_text: str = ""
    modality: str = UNKNOWN_MODALITY
    problems: list[str] = field(default_factory=list)
    enforceable: bool = True
    reason: str = ""


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


# ---- splitting --------------------------------------------------------------------------------------------------------
_CJK = re.compile(r"[　-鿿豈-﫿＀-￯]")
_SENT_SPLIT = re.compile(r"(?<=[。；;！？!?])|(?<!\d)(?<=\.)(?=\s+[A-Z])")


def join_lines(text: str) -> str:
    """Lines of a PDF paragraph / table into one string: no space between CJK characters, one space otherwise."""
    out = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if out and not (_CJK.match(out[-1]) or _CJK.match(line[0])):
            out += " "
        out += line
    return out


def split_sentences(text: str) -> list[str]:
    """Sentences of an evidence paragraph, verbatim (only the line breaks inside the paragraph are removed)."""
    joined = join_lines(text)
    return [s.strip() for s in _SENT_SPLIT.split(joined) if s and s.strip()]


def strip_numbering(sentence: str) -> str:
    return _NUMBERING.sub("", sentence, count=1).strip()


# ---- numbers ----------------------------------------------------------------------------------------------------------
def to_mm(value: float, unit: str) -> float:
    """Exact decimal conversion (0.3 m -> 300 mm; 1 in -> 25.4 mm)."""
    return float((Decimal(repr(value)) * UNIT_TO_MM[unit]).quantize(Decimal("0.000001")).normalize())


def canonical_unit(word: str | None) -> str | None:
    return UNIT_WORDS.get(word.lower()) if word else None


def _qty(m: re.Match, text: str) -> tuple[float, str | None, str]:
    num = float(m.group(1).replace(",", ""))
    unit = canonical_unit(m.group(2))
    if unit == "in" and re.match(r"\s+[A-Za-z]", text[m.end():]):
        unit = None                       # "300 in the drawing", not inches
    return num, unit, m.group(0).strip()


def zh_count(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if len(token) == 1:
        return ZH_DIGITS.get(token)
    if len(token) == 2 and token[0] == "十":
        return 10 + ZH_DIGITS.get(token[1], 0) if token[1] in ZH_DIGITS else None
    if len(token) == 2 and token[1] == "十":
        return ZH_DIGITS[token[0]] * 10 if token[0] in ZH_DIGITS else None
    if len(token) == 3 and token[1] == "十" and token[0] in ZH_DIGITS and token[2] in ZH_DIGITS:
        return ZH_DIGITS[token[0]] * 10 + ZH_DIGITS[token[2]]
    return None


def find_direction(nf: str) -> tuple[str | None, str | None, list[str], re.Match | None]:
    """(operator, matched words, problems, match). Postfix forms ``300 mm 以上`` are handled by ``find_limit``."""
    hits = list(_DIR_RE.finditer(nf))
    ops = {">=" if h.group(0).lower() in {w.lower() for w in GE_WORDS} else "<=" for h in hits}
    if len(ops) > 1:
        return None, None, ["BOTH_DIRECTIONS"], hits[0]
    if not hits:
        return None, None, [], None
    return ops.pop(), hits[0].group(0), [], hits[0]


def find_limit(nf: str, kind: str) -> tuple[str | None, float | None, str | None, str, list[str]]:
    """Comparison operator, value, unit and the number as written, for a length/angle (kind='length'|'angle') or a
    count (kind='count'). Returns problems instead of guessing."""
    problems: list[str] = []
    op, words, dprob, dmatch = find_direction(nf)
    problems += dprob
    post = _POSTFIX_RE.search(nf)
    if op is None and not dprob and post:
        op = ">=" if post.group(3) == "以上" else "<="
        if post.group(3) == "以外":
            op = None
            problems.append("NO_DIRECTION")
        num, unit, text = _qty(post, nf)
        return _finish_limit(op, num, unit, text + post.group(3), kind, problems)
    if op is None and not dprob:
        problems.append("NO_DIRECTION")
    if kind == "count":
        m = COUNT_RE.search(nf, dmatch.end() if dmatch else 0) or COUNT_RE.search(nf)
        if not m:
            return op, None, None, "", problems + ["NO_VALUE"]
        n = zh_count(m.group(1))
        if n is None:
            return op, None, None, m.group(0), problems + ["NO_VALUE"]
        return op, float(n), "count", m.group(0), problems
    start = dmatch.end() if dmatch else 0
    m = QTY_RE.search(nf, start)
    if m is None or (dmatch and m.start() - start > 30):
        m = QTY_RE.search(nf)             # the number before the words ("300 mm 不得小於…" is rare but exists)
    if m is None:
        return op, None, None, "", problems + ["NO_VALUE"]
    num, unit, text = _qty(m, nf)
    return _finish_limit(op, num, unit, text, kind, problems)


def _finish_limit(op, num, unit, text, kind, problems):
    if kind == "angle":
        if unit is None:
            problems.append("NO_UNIT")
            return op, num, None, text, problems
        if unit != "deg":
            problems.append("UNIT_MISMATCH")
            return op, num, unit, text, problems
        return op, num, "deg", text, problems
    if unit is None:
        problems.append("NO_UNIT")                            # never assume mm
        return op, num, None, text, problems
    if unit == "deg":
        problems.append("UNIT_MISMATCH")
        return op, num, unit, text, problems
    return op, to_mm(num, unit), "mm", text, problems


# ---- parties ----------------------------------------------------------------------------------------------------------
def _clean_part(token: str) -> str:
    t = token.strip(" ,，、")
    t = _LEAD.sub("", t)
    t = _PART_LEAD.sub("", t)
    prev = None
    while prev != t:
        prev = t
        t = _TRAIL.sub("", t).strip()
    t = re.sub(r"(?:之|的|等|與|和|及|跟)$", "", t).strip()
    return t


def last_clause(prefix: str) -> str:
    parts = [p for p in re.split(r"[,:;]", prefix) if p.strip()]
    while len(parts) > 1 and len(_clean_part(parts[-1])) <= 1:
        parts.pop()
    return parts[-1] if parts else ""


_VERBISH = re.compile(r"應|須|不得|不應|不宜|預留|保持|維持|滿足|考慮|必須|需要|宜|可|得|至少|最小|最大|不小於|不大於|"
                      r"以上|以下|以內|時|若|如|避免|發生|^為")


def plausible(text: str, limit: int = 24) -> str:
    """A phrase that can name drawing objects ("通信線纜架"), not a clause with a verb or a number in it."""
    t = (text or "").strip()
    if not t or len(t) > limit or _VERBISH.search(t) or any(canonical_unit(m.group(2)) for m in QTY_RE.finditer(t)):
        return ""
    return t


def split_parties(prefix: str) -> tuple[str, str, list[str]]:
    """'通信線纜架與電力線纜架之' -> ('通信線纜架', '電力線纜架', [])."""
    text = _clean_part(strip_numbering(last_clause(prefix)))
    found = list(CONNECTORS.finditer(text))
    if len(found) == 1:
        a, b = plausible(_clean_part(text[:found[0].start()])), plausible(_clean_part(text[found[0].end():]))
        return a, b, ([] if b else ["TARGET_MISSING"])
    if len(found) > 1:
        return plausible(text, 60), "", ["AMBIGUOUS_PARTIES"]
    return plausible(text), "", ["TARGET_MISSING"]


def _modality(nf: str) -> str:
    if SOFTENERS.search(nf):
        return ADVISORY
    if MANDATORY_RE.search(nf):
        return MANDATORY
    if ADVISORY_RE.search(nf):
        return ADVISORY
    return UNKNOWN_MODALITY


def _has_quantity(nf: str) -> bool:
    return any(canonical_unit(m.group(2)) for m in QTY_RE.finditer(nf)) or bool(COUNT_RE.search(nf))


def _has_length(nf: str) -> bool:
    return any(canonical_unit(m.group(2)) not in (None, "deg") for m in QTY_RE.finditer(nf))


# ---- one sentence -> candidates -----------------------------------------------------------------------------------------
def parse_sentence(sentence: str) -> list[Parsed]:
    """Zero, one or (for "horizontal ... , vertical ..." sentences) two parsed requirements."""
    if not sentence or len(sentence) > MAX_SENTENCE:
        return []
    nf = nfkc(strip_numbering(_MD_LEAD.sub("", sentence))).replace("**", "").replace("`", "")
    if not nf:
        return []
    h, v = H_CLEAR.search(nf), V_CLEAR.search(nf)
    if h and v and v.group(1) and h.start() != v.start():
        parts = _split_multi(nf)
        if parts:
            return parts
    one = _parse_one(nf, _modality(nf))
    return [one] if one else []


def _split_multi(nf: str) -> list[Parsed]:
    """Two requirements in one sentence (horizontal clearance, then vertical clearance). A part that names no parties
    inherits the first part's and is marked INHERITED_PARTIES (a person confirms the guess)."""
    modality = _modality(nf)
    out: list[Parsed] = []
    for piece in (p for p in re.split(r"[,;]", nf) if p.strip()):
        v = V_CLEAR.search(piece)
        if not (H_CLEAR.search(piece) or (v and v.group(1))):
            continue
        one = _parse_one(piece, modality)
        if one is None:
            continue
        if out and not one.subject_text:
            one.subject_text, one.target_text = out[0].subject_text, out[0].target_text
            one.problems = [x for x in one.problems if x not in ("SUBJECT_MISSING", "TARGET_MISSING")]
            one.problems.append("INHERITED_PARTIES")
        out.append(one)
    return out if len(out) >= 2 and len({o.template_id for o in out}) >= 2 else []


def _modal_problems(modality: str) -> list[str]:
    if modality == ADVISORY:
        return ["ADVISORY"]
    if modality == UNKNOWN_MODALITY:
        return ["NO_MODAL"]
    return []


UNSUPPORTED_DIRECTION_REASON = "原文的比較方向（例如「至少／不得小於」）目前沒有對應的規則類型可以檢查，僅供人工查核。"
VAGUE_REASON = "用語為「適當／方便／整齊／儘量」等，沒有可量化的判準，2D 圖面無法自動判定。"
UNSUPPORTED_REASON = "這句規範沒有現有規則類型可表達的檢查（距離、交叉、區域、長度、角度、數量），僅供人工查核。"


def _parse_one(nf: str, modality: str) -> Parsed | None:
    vague = bool(VAGUE_RE.search(nf))
    quantified = _has_quantity(nf)

    # --- not crossing
    for rx in (NO_CROSS_C, NO_CROSS_A):
        m = rx.search(nf)
        if m:
            a, b = plausible(_clean_part(strip_numbering(last_clause(m.group(1))))), plausible(_clean_part(m.group(2)))
            return _no_cross(a, b, [], modality)
    m = NO_CROSS_B.search(nf)
    if m:
        a, b, bad = split_parties(m.group(1))
        return _no_cross(a, b, bad, modality)

    # --- keep out of / stay inside a zone
    m = KEEP_OUT.search(nf)
    if m and m.group(2).strip():
        zone = plausible(_clean_part(re.split(r"[,;]", m.group(2))[0]), 40)
        return _zone("keep_out", "outside_zone", plausible(_clean_part(strip_numbering(last_clause(m.group(1))))), zone, modality)
    m = INSIDE_ZONE.search(nf)
    if m:
        return _zone("inside_zone", "inside_zone", plausible(_clean_part(strip_numbering(last_clause(m.group(1))))),
                     plausible(_clean_part(m.group(2)), 40), modality)

    # --- clearance in a stated direction
    h, v = H_CLEAR.search(nf), V_CLEAR.search(nf)
    v = v if v and (v.group(1) or "高程差" in v.group(0)) else None
    kw = h or v
    if kw:
        tid, meas = ("clearance", "horizontal_clearance") if kw is h else ("vertical_clearance", "vertical_clearance")
        subj, tgt, pparts = split_parties(nf[:kw.start()])
        return _limit_rule(nf, tid, meas, subj, tgt, pparts, modality, vague, ">=")

    # --- length of one object
    m = LENGTH_KW.search(nf)
    if m and not GENERIC_DIST.search(nf):
        subj = re.sub(r"(?:單一|單段|單根|單支|每一段|每段|每根|每支|每一)$", "", _clean_part(last_clause(nf[:m.start()])))
        return _limit_rule(nf, "max_length", "length", plausible(_clean_part(subj)), "", [], modality, vague, "<=")

    # --- horizontal / vertical only
    m = ORTHO_KW.search(nf)
    if m and modality != UNKNOWN_MODALITY:
        subj = plausible(_clean_part(strip_numbering(last_clause(nf[:m.start()]))))
        probs: list[str] = []
        op = val = unit = None
        text = ""
        if re.search(r"偏差|誤差|偏離|傾斜|角度", nf):
            op, val, unit, text, probs = find_limit(nf, "angle")
            probs = [p for p in probs if p != "NO_VALUE"]
        if val is None:
            probs.append("MISSING_TOLERANCE")
        if op == ">=":
            return None if modality == UNKNOWN_MODALITY else _not_enforceable(UNSUPPORTED_DIRECTION_REASON, modality)
        return _mk("orthogonal", "off_axis_angle", "<=", val, unit if val is not None else None, text, subj, "", "",
                   modality, _modal_problems(modality) + probs + ([] if subj else ["SUBJECT_MISSING"]))

    # --- at least N objects
    m = re.search(r"至少", nf)
    cm = COUNT_RE.search(nf, m.end()) if m else None
    if cm:
        after = re.sub(r"^(?:設置|設|有|裝設|配置|佈設|布設|安裝)", "", nf[cm.end():].lstrip())
        subj = plausible(_clean_part(re.split(r"[,;]", after)[0])) or plausible(_clean_part(
            re.sub(r"(?:應|須|必須|需)?(?:設置|設|有|裝設|配置)?$", "", nf[:m.start()])))
        n = zh_count(cm.group(1))
        return _mk("min_count", "entity_count", ">=", float(n) if n is not None else None,
                   "count" if n is not None else None, cm.group(0), subj, "", "", modality,
                   _modal_problems(modality) + ([] if n is not None else ["NO_VALUE"]) + ([] if subj else ["SUBJECT_MISSING"]))

    # --- distance without a stated axis, or "keep N mm"
    gm = GENERIC_DIST.search(nf)
    sv = SPACING_VERB.search(nf)
    if gm or (sv and _has_length(nf) and modality != UNKNOWN_MODALITY):
        if vague and not quantified:
            return _not_enforceable(VAGUE_REASON, modality)
        pos = (gm or sv).start()
        subj, tgt, pparts = split_parties(nf[:pos])
        op, val, unit, text, probs = find_limit(nf, "length")
        if val is None and modality == UNKNOWN_MODALITY:
            return None
        if op == "<=":                                   # "distance to the nearest <target> not more than N"
            return _mk("max_distance", "distance", "<=", val, unit, text, subj, tgt, "", modality,
                       _modal_problems(modality) + probs + pparts + ([] if subj else ["SUBJECT_MISSING"]))
        return _mk("clearance", "horizontal_clearance", op, val, unit, text, subj, tgt, "", modality,
                   _modal_problems(modality) + probs + pparts + ["UNSPECIFIED_AXIS"] + ([] if subj else ["SUBJECT_MISSING"]))

    # --- layout wording only: kept as "not checkable", never as a rule
    if modality != UNKNOWN_MODALITY and ROUTE_RE.search(nf) and not DOC_PROSE_RE.search(nf) \
            and (vague or not quantified) and LAYOUT_VERB_RE.search(nf):
        if vague or (re.search(r"不得|應|須|必須|禁止|嚴禁", nf) and len(nf) >= 8):
            return _not_enforceable(VAGUE_REASON if vague else UNSUPPORTED_REASON, modality)
    return None


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for i in items:
        if i not in seen:
            seen.append(i)
    return seen


SINGLE_SUBJECT = {"keep_out", "inside_zone", "max_length", "orthogonal", "min_count"}


def _mk(tid, meas, op, val, unit, text, subj, tgt, zone, modality, problems) -> Parsed:
    if tid in SINGLE_SUBJECT and CONNECTORS.search(subj or ""):      # "A跟B不應穿越…": several things, one slot
        problems = [*problems, "AMBIGUOUS_PARTIES"]
    return Parsed(template_id=tid, measurement=meas, operator=op, value=val, unit=unit, value_text=text,
                  subject_text=subj, target_text=tgt, zone_text=zone, modality=modality,
                  problems=_dedupe(problems), enforceable=True)


def _not_enforceable(reason: str, modality: str) -> Parsed:
    return Parsed(None, None, None, None, None, "", modality=modality, problems=[], enforceable=False, reason=reason)


def _no_cross(a: str, b: str, bad: list[str], modality: str) -> Parsed:
    probs = _modal_problems(modality) + bad
    if not a:
        probs.append("SUBJECT_MISSING")
    if not b and "AMBIGUOUS_PARTIES" not in probs:
        probs.append("TARGET_MISSING")
    return _mk("no_crossing", "intersection", "==", 0.0, "count", "", a, b, "", modality, probs)


def _zone(tid: str, meas: str, subj: str, zone: str, modality: str) -> Parsed:
    probs = _modal_problems(modality)
    if tid == "keep_out" and re.search(r"[內中裡間處]$", subj):
        probs.append("AMBIGUOUS_PARTIES")            # "機房內嚴禁穿越水管": the room is the place, not the thing that moves
    if not subj:
        probs.append("SUBJECT_MISSING")
    if not zone:
        probs.append("ZONE_MISSING")
    return _mk(tid, meas, "==", 1.0, "count", "", subj, "", zone, modality, probs)


def _limit_rule(nf, tid, meas, subj, tgt, pparts, modality, vague, want_op) -> Parsed:
    """A rule that compares a measured length with a limit (clearances, maximum length)."""
    op, val, unit, text, probs = find_limit(nf, "length")
    if vague and val is None:
        return _not_enforceable(VAGUE_REASON, modality)
    if val is None and modality == UNKNOWN_MODALITY:       # no number and no "shall": prose that merely mentions the word
        return None
    if modality == UNKNOWN_MODALITY and op is None and unit is None:       # "... 2 件 ...": a number, but not a requirement
        return None
    if op and op != want_op:
        return None if modality == UNKNOWN_MODALITY else _not_enforceable(UNSUPPORTED_DIRECTION_REASON, modality)
    out = _modal_problems(modality)
    out += probs + pparts
    if not subj:
        out.append("SUBJECT_MISSING")
    return _mk(tid, meas, op, val, unit, text, subj, tgt, "", modality, out)
