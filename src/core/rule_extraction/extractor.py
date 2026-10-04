"""Evidence paragraphs -> raw rule candidates (one per requirement sentence, verbatim sentence kept)."""
from __future__ import annotations

from typing import Iterable

from core.models.results import EvidenceChunk
from .candidate import RawCandidate, build_semantic_key, norm_text
from .patterns import Parsed, parse_sentence, split_sentences

EXTRACTOR_VERSION = 1      # bump when the reading rules change: stored candidates are then re-extracted


def raw_from_parsed(p: Parsed, sentence: str, chunk: EvidenceChunk, document_id: str, document: str) -> RawCandidate:
    complete = p.enforceable and not p.problems
    key = build_semantic_key(p.measurement, p.operator, p.value, p.unit, p.subject_text, p.target_text, p.zone_text,
                             sentence, complete)
    family = "|".join([p.measurement or "?", norm_text(p.subject_text), norm_text(p.target_text) or norm_text(p.zone_text)])
    return RawCandidate(
        semantic_key=key, family_key_text=family, template_id=p.template_id, measurement=p.measurement,
        operator=p.operator, value=p.value, unit=p.unit, value_text=p.value_text, subject_text=p.subject_text,
        target_text=p.target_text, zone_text=p.zone_text, modality=p.modality, problems=list(p.problems),
        enforceable=p.enforceable, sentence=sentence, reason=p.reason, document_id=document_id, document=document,
        evidence_id=chunk.id, page=chunk.page, line_start=chunk.line_start, line_end=chunk.line_end,
        section=chunk.section or "")


def extract_from_chunks(chunks: Iterable[EvidenceChunk], document_id: str, document: str) -> list[RawCandidate]:
    """All candidates of one document. The same meaning stated twice in one document is kept once (first wins)."""
    seen: set[str] = set()
    out: list[RawCandidate] = []
    for chunk in chunks:
        for sentence in split_sentences(chunk.text):
            for parsed in parse_sentence(sentence):
                raw = raw_from_parsed(parsed, sentence, chunk, document_id, document)
                if raw.semantic_key in seen:
                    continue
                seen.add(raw.semantic_key)
                out.append(raw)
    return out
