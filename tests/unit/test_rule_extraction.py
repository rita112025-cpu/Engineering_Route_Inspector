"""Specification sentence -> rule candidate: reading rules, statuses, selector mapping, conflicts, stable identity."""
from __future__ import annotations

import pytest

from core.evidence.chunks import evidence_id, sha256_text
from core.models.results import EvidenceChunk
from core.rule_extraction.candidate import (CONFIRMED, CONFLICTING, NEEDS_REVIEW, NOT_ENFORCEABLE, stable_candidate_id)
from core.rule_extraction.extractor import extract_from_chunks
from core.rule_extraction.patterns import parse_sentence, split_sentences
from core.rule_extraction.validator import MappingContext, effective_glossary, evaluate, resolve_term

LAYERS = ["COMM-CABLE", "POWER-CABLE", "SCADA-CABLE", "WATER-PIPE", "ZONE-NOGO"]


def one(sentence: str):
    parsed = parse_sentence(sentence)
    assert len(parsed) == 1, parsed
    return parsed[0]


def chunk(text: str, doc="doc_0000000000000001", name="spec.md", page=1, line=1) -> EvidenceChunk:
    return EvidenceChunk(id=evidence_id("h" * 64, page, line, text), document_id=doc, filename=name, page=page,
                         line_start=line, line_end=line, section="附錄 C", text=text, hash=sha256_text(text))


def views(texts, layers=LAYERS, systems=(), decisions=None, choices=None, glossary=None, docs=None):
    chunks = [chunk(t, doc=(docs[i] if docs else "doc_0000000000000001"), name=f"s{i}.md" if docs else "spec.md",
                    line=i + 1) for i, t in enumerate(texts)]
    records = []
    for c in chunks:
        for raw in extract_from_chunks([c], c.document_id, c.filename):
            records.append({"id": stable_candidate_id(c.document_id, raw.semantic_key), "raw": raw})
    ctx = MappingContext(layers=list(layers), systems=list(systems), glossary=effective_glossary(glossary))
    return evaluate(records, ctx, decisions or {}, choices or {}, {c.id: c.text for c in chunks})


# ---- numbers, units, direction ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,op,value,unit", [
    ("水平淨距不得小於 300 mm", ">=", 300, "mm"),
    ("水平淨距不得少於300毫米", ">=", 300, "mm"),
    ("水平淨距至少 30 公分", ">=", 300, "mm"),
    ("水平淨距不小於 0.3 m", ">=", 300, "mm"),
    ("水平淨距應在 300 mm 以上", ">=", 300, "mm"),
    ("水平淨距應為最小 1 ft", ">=", 304.8, "mm"),
    ("單段長度不得大於 500 mm", "<=", 500, "mm"),
    ("單段長度不得超過 1.5 公尺", "<=", 1500, "mm"),
    ("單段長度應在 2000 mm 以內", "<=", 2000, "mm"),
])
def test_direction_value_and_unit_are_read_not_defaulted(text, op, value, unit):
    p = one(text)
    assert (p.operator, p.value, p.unit) == (op, value, unit)
    assert not [x for x in p.problems if x in ("NO_UNIT", "NO_DIRECTION", "NO_VALUE")]


def test_at_least_n_objects():
    p = one("圖面中至少設置 3 個拉線箱")
    assert (p.template_id, p.operator, p.value, p.unit) == ("min_count", ">=", 3, "count")
    assert one("至少設置三個拉線箱").value == 3


def test_not_larger_than_is_less_or_equal():
    p = one("管線距離最近之支架不得超過 500 mm")
    assert (p.template_id, p.operator, p.value) == ("max_distance", "<=", 500)


def test_a_number_without_a_unit_is_never_millimetres():
    p = one("水平淨距不得小於 300")
    assert p.unit is None and "NO_UNIT" in p.problems


def test_no_direction_word_is_not_guessed():
    p = one("水平淨距為 300 mm")
    assert p.operator is None and "NO_DIRECTION" in p.problems
    both = parse_sentence("水平淨距不得小於 300 mm 且不得大於 500 mm")[0]
    assert both.operator is None and "BOTH_DIRECTIONS" in both.problems


# ---- check types ---------------------------------------------------------------------------------------------------------
def test_horizontal_and_vertical_clearance():
    h = one("通信線纜架與電力線纜架之水平淨距不得小於 300 mm")
    assert (h.template_id, h.measurement, h.subject_text, h.target_text) == \
        ("clearance", "horizontal_clearance", "通信線纜架", "電力線纜架")
    v = one("通信線纜架與電力線纜架之垂直淨距不得小於 150 mm")
    assert (v.template_id, v.measurement) == ("vertical_clearance", "vertical_clearance")


def test_no_crossing_forms():
    for s in ("弱電電纜不得與給水管交叉", "弱電電纜與給水管不得交叉", "禁止弱電電纜與給水管交叉"):
        p = one(s)
        assert (p.template_id, p.measurement, p.operator, p.value) == ("no_crossing", "intersection", "==", 0)
        assert (p.subject_text, p.target_text) == ("弱電電纜", "給水管") and not p.problems


def test_zone_rules():
    k = one("弱電電纜不得進入機房禁設區")
    assert (k.template_id, k.subject_text, k.zone_text) == ("keep_out", "弱電電纜", "機房禁設區")
    z = one("弱電電纜必須位於電纜走廊範圍內")
    assert (z.template_id, z.zone_text) == ("inside_zone", "電纜走廊")


def test_orthogonal_needs_a_stated_tolerance():
    assert "MISSING_TOLERANCE" in one("管線應水平或垂直配置").problems
    p = one("管線應水平或垂直配置，偏差不得超過 1°")
    assert (p.template_id, p.value, p.unit, p.problems) == ("orthogonal", 1, "deg", [])


def test_axis_must_be_said():
    p = one("管線間距不得小於 300 mm")
    assert "UNSPECIFIED_AXIS" in p.problems     # not silently horizontal


def test_two_requirements_in_one_sentence_are_split_and_the_second_is_flagged():
    a, b = parse_sentence("通信線纜架與電力線纜架之水平淨距不得小於 300 mm，垂直淨距不得小於 150 mm")
    assert (a.value, b.value) == (300, 150) and "INHERITED_PARTIES" in b.problems


def test_several_parties_in_one_sentence_are_ambiguous():
    assert "AMBIGUOUS_PARTIES" in one("電纜架與水管及風管之水平淨距不得小於 300 mm").problems


# ---- wording -------------------------------------------------------------------------------------------------------------
def test_vague_wording_is_not_enforceable():
    for s in ("應保持適當距離", "設備應設置於方便維修之位置", "電纜應整齊配置", "應儘量縮短路徑"):
        p = one(s)
        assert p.enforceable is False and p.template_id is None, s


def test_advisory_wording_never_becomes_a_fail_rule():
    for s in ("建議保持 300 mm", "應儘量維持 300 mm", "水平淨距宜大於 300 mm", "通信線纜架與電力線纜架之水平淨距建議不得小於 300 mm"):
        v = views([s])
        assert len(v) == 1 and v[0].status in (NEEDS_REVIEW, NOT_ENFORCEABLE), s
        assert not v[0].enabled
    v = views(["通信線纜架與電力線纜架之水平淨距建議不得小於 300 mm"])[0]
    assert "ADVISORY" in v.problems


def test_accepting_an_advisory_sentence_makes_a_warning_not_a_fail():
    text = "通信線纜架與電力線纜架之水平淨距宜不得小於 300 mm"
    v = views([text])[0]
    assert v.status == NEEDS_REVIEW
    v2 = views([text], decisions={v.id: {"accept": True}})[0]
    assert (v2.status, v2.severity, v2.confirmed_by) == (CONFIRMED, "WARNING", "user")


def test_sentences_keep_the_original_wording():
    assert split_sentences("通信線纜架與電力線纜架\n水平淨距不得小於 300 mm。\n另一句。") == \
        ["通信線纜架與電力線纜架水平淨距不得小於 300 mm。", "另一句。"]


def test_decimals_do_not_split_sentences():
    assert len(split_sentences("The clearance shall not be less than 0.3 m. Next sentence here.")) == 2
    assert len(split_sentences("水平淨距不得小於 0.3 m")) == 1


# ---- selector mapping ------------------------------------------------------------------------------------------------------
SENTENCE = "通信線纜架與電力線纜架之水平淨距不得小於 300 mm"


def test_a_single_mapping_confirms():
    v = views([SENTENCE])[0]
    assert v.status == CONFIRMED and v.enabled and v.confirmed_by == "auto"
    assert v.subject_selector == {"layer": "COMM-CABLE"} and v.target_selector == {"layer": "POWER-CABLE"}
    assert (v.operator, v.value, v.unit, v.severity) == (">=", 300, "mm", "FAIL")


def test_no_mapping_needs_review_and_does_not_guess():
    v = views([SENTENCE], layers=["POWER-CABLE", "WATER-PIPE"])[0]
    assert v.status == NEEDS_REVIEW and not v.enabled and v.subject_selector is None
    assert "SUBJECT_UNMAPPED" in v.problems and v.fix["slots"] == ["subject"]


def test_several_mappings_need_review():
    v = views([SENTENCE], systems=[{"name": "通信", "layer_regex": "^COMM"}],
              layers=LAYERS + ["TEL-CABLE"], glossary={"通信": ["COMM-*"], "纜架": ["TEL-*"]})[0]
    assert v.status == NEEDS_REVIEW and "SUBJECT_UNMAPPED" in v.problems      # two different answers: no pick


def test_an_exact_word_beats_a_contained_word():
    v = views([SENTENCE], layers=LAYERS + ["TEL-CABLE"], glossary={"通信": ["COMM-*"], "通信線纜架": ["TEL-*"]})[0]
    assert v.subject_selector == {"layer": "TEL-CABLE"}


def test_a_system_of_the_project_is_a_mapping():
    v = views([SENTENCE], systems=[{"name": "通信", "layer_regex": "^COMM"}, {"name": "電力", "layer_regex": "^POWER"}])[0]
    assert v.status == CONFIRMED and v.subject_selector == {"system": "通信"} and v.target_selector == {"system": "電力"}


def test_no_drawing_means_no_confirmation():
    v = views([SENTENCE], layers=[])[0]
    assert v.status == NEEDS_REVIEW and "NO_DRAWING" in v.problems


def test_resolve_term_tiers():
    ctx = MappingContext(layers=LAYERS, systems=[], glossary=effective_glossary(None))
    assert resolve_term("SCADA-CABLE", ctx) == ({"layer": "SCADA-CABLE"}, "ok")      # the layer itself
    assert resolve_term("給水管", ctx)[1] == "none"
    assert resolve_term("通信及電力共同管架", ctx)[1] == "multiple"


def test_a_person_picking_the_missing_object_confirms():
    v = views([SENTENCE], layers=["POWER-CABLE"])[0]
    v2 = views([SENTENCE], layers=["POWER-CABLE"], decisions={v.id: {"selectors": {"subject": {"layer": "POWER-CABLE"}}}})[0]
    assert v2.status == CONFIRMED and v2.confirmed_by == "user"
    bogus = views([SENTENCE], layers=["POWER-CABLE"], decisions={v.id: {"selectors": {"subject": {"layer": "NOPE"}}}})[0]
    assert bogus.status == NEEDS_REVIEW                     # a layer the drawing does not have is not accepted


def test_missing_unit_can_only_be_fixed_by_a_person_choosing_one():
    text = "通信線纜架與電力線纜架之水平淨距不得小於 300"
    v = views([text])[0]
    assert v.status == NEEDS_REVIEW and "NO_UNIT" in v.problems
    fixed = views([text], decisions={v.id: {"unit": "cm"}})[0]
    assert (fixed.status, fixed.value, fixed.unit) == (CONFIRMED, 3000, "mm")


# ---- evidence ---------------------------------------------------------------------------------------------------------------
def test_no_evidence_no_confirmation():
    chunks = [chunk(SENTENCE)]
    raw = extract_from_chunks(chunks, chunks[0].document_id, "spec.md")[0]
    ctx = MappingContext(layers=LAYERS, glossary=effective_glossary(None))
    v = evaluate([{"id": "cand_x", "raw": raw}], ctx, {}, {}, {})[0]
    assert v.status == NEEDS_REVIEW and "EVIDENCE_MISSING" in v.problems
    tampered = evaluate([{"id": "cand_x", "raw": raw}], ctx, {}, {}, {chunks[0].id: "完全不同的文字"})[0]
    assert "EVIDENCE_MISMATCH" in tampered.problems


def test_the_evidence_is_kept_verbatim():
    v = views(["本工程中，" + SENTENCE + "。"])[0]
    assert v.raw.sentence.endswith("300 mm。") and v.raw.evidence_id.startswith("EV-")
    assert (v.raw.document, v.raw.page, v.raw.section) == ("spec.md", 1, "附錄 C")


# ---- conflicts ----------------------------------------------------------------------------------------------------------------
def test_different_values_for_the_same_objects_conflict():
    a = "通信線纜架與電力線纜架之水平淨距不得小於 300 mm"
    b = "通信線纜架與電力線纜架之水平淨距不得小於 500 mm"
    vs = views([a, b], docs=["doc_000000000000000a", "doc_000000000000000b"])
    assert [v.status for v in vs] == [CONFLICTING, CONFLICTING]
    assert not any(v.enabled for v in vs)
    assert {o["value"] for o in vs[0].conflict_options} == {300, 500}
    assert {o["source"]["document"] for o in vs[0].conflict_options} == {"s0.md", "s1.md"}   # both evidences kept


def test_a_conflict_is_resolved_only_by_a_person_and_never_by_value_or_date():
    a = "通信線纜架與電力線纜架之水平淨距不得小於 300 mm"
    b = "通信線纜架與電力線纜架之水平淨距不得小於 500 mm"
    docs = ["doc_000000000000000a", "doc_000000000000000b"]
    vs = views([a, b], docs=docs)
    pick = vs[0]
    chosen = views([a, b], docs=docs, choices={pick.conflict_group: pick.raw.semantic_key})
    assert [(v.status, v.enabled, v.bucket) for v in chosen] == [(CONFIRMED, True, "usable"), (CONFLICTING, False, "off")]
    assert chosen[0].value == 300                  # the one the person chose, not the stricter or the later one


def test_same_value_in_two_documents_is_a_duplicate_not_a_conflict():
    s = "通信線纜架與電力線纜架之水平淨距不得小於 300 mm"
    vs = views([s, s], docs=["doc_000000000000000a", "doc_000000000000000b"])
    assert len(vs) == 1 and vs[0].status == CONFIRMED and len(vs[0].duplicates) == 1


def test_different_objects_do_not_conflict():
    vs = views(["通信線纜架與電力線纜架之水平淨距不得小於 300 mm", "SCADA 線纜架與電力線纜架之水平淨距不得小於 500 mm"])
    assert [v.status for v in vs] == [CONFIRMED, CONFIRMED]


# ---- identity ------------------------------------------------------------------------------------------------------------------
def test_ids_survive_paragraph_movement_and_rescans():
    a = views(["前一段說明文字。", SENTENCE])[0]
    b = views([SENTENCE, "其他文字", "更多文字"])[0]
    assert a.id == b.id and a.id.startswith("cand_")
    other_doc = views([SENTENCE], docs=["doc_000000000000000f"])[0]
    assert other_doc.id != a.id                 # identity = document content hash + meaning: another document, another ID


def test_wording_changes_with_the_same_meaning_keep_the_key():
    k1 = views(["通信線纜架與電力線纜架之水平淨距不得小於 300 mm"])[0].raw.semantic_key
    k2 = views(["通信線纜架與電力線纜架間之水平間距不得少於 0.3 公尺"])[0].raw.semantic_key
    assert k1 == k2


# ---- prose that only mentions the words (found by reading real documents) ----------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Layer E 之線索須先回查 Source／Derived Evidence 並驗證成功，始得進入 Findings。",
    "### 判讀界線（不得逾越）",
    "- 「SCADA／通訊設備 Room」為白板用語，**不得逕自等同於 TER／SER** 或任何既有機房代號。",
    "任何線索須回查 Source／Derived Evidence 驗證成功後，始得進入 Findings。",
    "FIND-015 關於 EQD-ESC-0007 表3-2 與 SoW V6「44 台」對應關係之部分不受影響，仍予保留。",
    "（差額 2 件係路徑長度超過 260 字元之列舉限制，非檔案缺失；",
    "原 11 欄位全數保留",
    "Rack 21U 機架註記 → 機架 U 數第三值",
])
def test_prose_that_only_mentions_the_words_yields_nothing(text):
    assert parse_sentence(text) == []


def test_markdown_decoration_does_not_change_the_reading():
    p = one("- **通信線纜架與電力線纜架之水平淨距不得小於 300 mm**")
    assert (p.subject_text, p.target_text, p.value) == ("通信線纜架", "電力線纜架", 300)


@pytest.mark.parametrize("text,op", [("通信線纜架與電力線纜架之水平淨距不應小於 300mm", ">="),
                                      ("通信線纜架與電力線纜架之水平淨距不可少於 300mm", ">="),
                                      ("單段長度不應超過 3000 mm", "<="), ("單段長度不能大於 3000 mm", "<=")])
def test_negated_comparisons_have_the_right_direction(text, op):
    p = one(text)
    assert p.operator == op and "NO_DIRECTION" not in p.problems and "ADVISORY" not in p.problems


def test_not_advisable_is_advisory_not_mandatory():
    p = one("通信線纜架與電力線纜架之水平淨距不宜小於 300 mm")
    assert p.operator == ">=" and p.modality == "advisory" and "ADVISORY" in p.problems


def test_clauses_are_not_taken_as_names_of_objects():
    p = one("確實困難時，應預留不小於300mm 之間距。")
    assert (p.subject_text, p.target_text) == ("", "") and "SUBJECT_MISSING" in p.problems
    p = one("在需要檢修之一側管線之間應預留不小於400mm 之間距")
    assert p.subject_text == "" and p.value == 400


def test_an_unspecified_axis_is_not_shown_as_horizontal():
    v = views(["通信線纜架與電力線纜架之間距不得小於 300 mm"], layers=["COMM-CABLE", "POWER-CABLE"])[0]
    assert "UNSPECIFIED_AXIS" in v.problems and "水平或垂直未定" in v.requirement and "水平淨距" not in v.requirement
    fixed = views(["通信線纜架與電力線纜架之間距不得小於 300 mm"], layers=["COMM-CABLE", "POWER-CABLE"],
                  decisions={v.id: {"template_id": "vertical_clearance"}})[0]
    assert fixed.status == CONFIRMED and fixed.measurement == "vertical_clearance" and fixed.requirement.startswith("垂直淨距")


def test_enumerators_and_appendix_numbers_are_dropped_from_the_names():
    p = one("A. 外緣距離牆或樓板之間的距離不宜小於200mm。")
    assert p.value == 200 and "A" not in p.subject_text[:1]
    p = one("附錄C-8 (十三) 通信線纜架與電力線纜架之水平淨距不得小於 300 mm")
    assert (p.subject_text, p.target_text) == ("通信線纜架", "電力線纜架")


def test_a_direction_no_rule_type_can_check_is_not_enforceable_not_a_card_nobody_can_fix():
    p = one("通信線纜架與電力線纜架之水平淨距不得大於 300 mm")          # the clearance rule type only knows "not less than"
    assert p.enforceable is False and "沒有對應的規則類型" in p.reason
    assert parse_sentence("最小長度為 25 m") == []                        # no "shall" and no rule type: nothing to show


def test_a_person_can_choose_a_missing_direction_but_only_one_the_rule_type_can_check():
    text = "通信線纜架與電力線纜架之水平淨距應為 300 mm"
    v = views([text], layers=["COMM-CABLE", "POWER-CABLE"])[0]
    assert v.status == NEEDS_REVIEW and "NO_DIRECTION" in v.problems and v.fix["direction"] and v.fix["direction_ops"] == [">="]
    ok = views([text], layers=["COMM-CABLE", "POWER-CABLE"], decisions={v.id: {"operator": ">="}})[0]
    assert (ok.status, ok.operator, ok.confirmed_by) == (CONFIRMED, ">=", "user")
    wrong = views([text], layers=["COMM-CABLE", "POWER-CABLE"], decisions={v.id: {"operator": "<="}})[0]
    assert wrong.status == NEEDS_REVIEW and "NO_DIRECTION" in wrong.problems


# ---- found in the real specification: wrong conflicts, reversed zone sentences, partial unions -------------------------------------
def test_requirements_that_do_not_say_what_they_are_about_never_conflict():
    vs = views(["確實困難時，應預留不小於300mm 之間距。", "在需要檢修之一側管線之間應預留不小於400mm 之間距。",
                "至少設置 3 個", "至少設置 2 個"], layers=LAYERS)
    assert CONFLICTING not in {v.status for v in vs}


def test_a_location_phrase_is_not_taken_as_the_thing_that_moves():
    v = views(["電氣設備用房內嚴禁穿越各類水管。"], layers=["POWER-CABLE", "WATER-PIPE"], glossary={"水管": ["WATER-PIPE"], "電氣設備用房": ["POWER-CABLE"]})[0]
    assert v.status == NEEDS_REVIEW and "AMBIGUOUS_PARTIES" in v.problems          # never CONFIRMED the wrong way round
    assert v.fix["slots"] == ["subject", "zone"]
    ok = views(["電氣設備用房內嚴禁穿越各類水管。"], layers=["POWER-CABLE", "WATER-PIPE"],
               decisions={v.id: {"selectors": {"subject": {"layer": "WATER-PIPE"}, "zone": {"layer": "POWER-CABLE"}}}})[0]
    assert ok.status == CONFIRMED and ok.confirmed_by == "user"


def test_several_things_in_one_slot_are_not_silently_half_applied():
    glossary = {"電纜線架": ["POWER-CABLE"]}
    v = views(["無關的風管跟電纜線架不應穿越樓梯間。"], layers=["POWER-CABLE", "ZONE-NOGO"], glossary=glossary)[0]
    assert v.status == NEEDS_REVIEW and "AMBIGUOUS_PARTIES" in v.problems            # 風管 and 電纜線架: a person says which
    lst = views(["電纜線架、風管不應穿越樓梯間。"], layers=["POWER-CABLE", "DUCT-1", "ZONE-NOGO"],
                glossary={"電纜線架": ["POWER-CABLE"], "風管": ["DUCT-1"], "樓梯間": ["ZONE-NOGO"]})[0]
    assert lst.status == CONFIRMED and lst.subject_selector == {"layers": ["DUCT-1", "POWER-CABLE"]}    # a "、" list is a union
