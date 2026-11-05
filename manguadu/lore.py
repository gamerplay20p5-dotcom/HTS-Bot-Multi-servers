"""Triagem local inicial de lore; nao substitui interpretacao humana."""

from __future__ import annotations

from .i18n import t

import re
import unicodedata
from dataclasses import dataclass
import heapq


STOPWORDS = frozenset("""a ao aos as o os um uma uns umas de da do das dos e em na no nas nos
por para com sem que se eu ele ela meu minha seu sua seus suas era foi ser ter tinha
ha havia muito mais menos depois antes quando onde porque como sobre tambem ate
esse essa isso este esta isto eles elas num numa pelo pela pelos pelas entre
durante ainda ja nao sim ou mas so sua seu nossa nosso minha meu
the a an and or but in on at of to from for with without that this these those
my your his her their our was were is are been being have has had would could
should will shall then than when where what which who whom because after before
about into over under through some any much many more most very also until""".split())


def normalize_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {token for token in re.findall(r"[a-z0-9]{4,}", normalized) if token not in STOPWORDS}


@dataclass(frozen=True)
class LoreResult:
    approved: bool
    score: float
    common_terms: tuple[str, ...]
    reason: str
    summary: str = ""
    contradictions: tuple[str, ...] = ()
    source: str = "local"
    decision: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "score": self.score,
            "common_terms": list(self.common_terms),
            "reason": self.reason,
            "summary": self.summary,
            "contradictions": list(self.contradictions),
            "source": self.source,
            "decision": self.decision,
        }

    @classmethod
    def from_payload(cls, value: object) -> "LoreResult | None":
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                approved=bool(value["approved"]),
                score=max(0.0, min(1.0, float(value["score"]))),
                common_terms=tuple(str(item)[:240] for item in value.get("common_terms", []) if str(item).strip())[:12],
                reason=str(value.get("reason", ""))[:1200],
                summary=str(value.get("summary", ""))[:900],
                contradictions=tuple(str(item)[:240] for item in value.get("contradictions", []) if str(item).strip())[:8],
                source=str(value.get("source", "local"))[:40],
                decision=str(value.get("decision", ""))[:20],
            )
        except (KeyError, TypeError, ValueError):
            return None


MAX_LORE_CHARS = 9_999_999
LORE_CONTEXT_CHARS = 24_000
LORE_CONTEXT_CHUNK_CHARS = 1_600


def select_relevant_context(source: str, query: str, limit: int = LORE_CONTEXT_CHARS) -> str:
    """Select bounded passages without keeping an index for a huge lore file."""
    if len(source) <= limit:
        return source
    query_sample = query if len(query) <= 48_000 else _uniform_sample(query, 48_000)
    query_tokens = normalize_tokens(query_sample)
    chunk_size = LORE_CONTEXT_CHUNK_CHARS
    chunk_count = max(1, limit // (chunk_size + 20))
    diversity_count = min(chunk_count, max(1, chunk_count // 4))
    chunk_total = max(1, (len(source) + chunk_size - 1) // chunk_size)
    diverse_offsets = {
        round((chunk_total - 1) * index / max(1, diversity_count - 1)) * chunk_size
        for index in range(diversity_count)
    }
    best: list[tuple[int, int, str]] = [
        (-1, -offset, source[offset:offset + chunk_size])
        for offset in diverse_offsets
    ]
    heapq.heapify(best)
    selected_offsets = set(diverse_offsets)
    for offset in range(0, len(source), chunk_size):
        if offset in selected_offsets:
            continue
        chunk = source[offset:offset + chunk_size]
        score = len(query_tokens & normalize_tokens(chunk))
        if score:
            entry = (score, -offset, chunk)
            if len(best) < chunk_count:
                heapq.heappush(best, entry)
            elif entry > best[0]:
                heapq.heapreplace(best, entry)
    if not best:
        return _uniform_sample(source, limit)
    selected = sorted(((-offset, chunk) for _score, offset, chunk in best), key=lambda item: item[0])
    return t('\n\n[...trecho selecionado da lore...]\n\n').join(chunk for _offset, chunk in selected)[:limit]


def _uniform_sample(source: str, limit: int) -> str:
    if len(source) <= limit:
        return source
    segment_count = max(1, limit // LORE_CONTEXT_CHUNK_CHARS)
    segment_size = max(1, limit // segment_count)
    last_start = max(0, len(source) - segment_size)
    offsets = [round(last_start * index / max(1, segment_count - 1)) for index in range(segment_count)]
    return t('\n\n[...trecho omitido...]\n\n').join(source[offset:offset + segment_size] for offset in offsets)[:limit]


def evaluate_lore(reference: str, submission: str, minimum_score: float = 0.28) -> LoreResult:
    reference = select_relevant_context(reference, submission)
    submission = select_relevant_context(submission, reference)
    if len(submission.strip()) < 120:
        return LoreResult(False, 0.0, (), t('A história precisa ter pelo menos 120 caracteres.'))
    reference_tokens = normalize_tokens(reference)
    submission_tokens = normalize_tokens(submission)
    if len(reference_tokens) < 8:
        return LoreResult(False, 0.0, (), t('A lore base ainda precisa de mais detalhes para a triagem automática.'))
    if len(submission_tokens) < 12:
        return LoreResult(False, 0.0, (), t('Descreva melhor a história do personagem.'))
    common = reference_tokens & submission_tokens
    # Cobertura de termos da lore base e uma medida verificavel, mas lexical.
    score = len(common) / len(reference_tokens)
    approved = len(common) >= 3 and score >= minimum_score
    reason = t('A lore passou na triagem de consistência.') if approved else t('A história não traz referências suficientes à lore base.')
    return LoreResult(approved, round(score, 3), tuple(sorted(common)), reason)
