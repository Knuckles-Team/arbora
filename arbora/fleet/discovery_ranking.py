"""Lexical ranking of fleet discovery candidates (ARBORA-FLEET-R074).

``find`` ranks every tool and skill of the probed fleet against a natural
language request. A plain overlap ratio treats each query word alike, so on a
fleet of tens of thousands of tools a common word ("list", "get", "in") ties
thousands of candidates and the one that names the asked-for system and
object never reaches the top. This ranker drops stopwords, folds plurals and
weights each query term by how rare it is among the candidates, so the words
that discriminate ("seerr", "pending") decide the order. A match in the
candidate's own name counts double.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

__all__ = ["LexicalRanker", "terms"]

_WORD_RE = re.compile(r"[a-z0-9]+")
#: Request words that carry no routing signal.
_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "show",
        "that",
        "the",
        "this",
        "to",
        "what",
        "which",
        "with",
    }
)


def _fold(word: str) -> str:
    """One word's singular stem: ``requests`` and ``request`` match."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def terms(text: str) -> set[str]:
    """The routing terms of one text: lowercased, stopword-free, folded."""
    return {
        _fold(word)
        for word in _WORD_RE.findall(str(text or "").lower())
        if word not in _STOPWORDS
    }


class LexicalRanker:
    """Score candidates for one query against the candidate population."""

    def __init__(self, query: str, documents: Iterable[str]) -> None:
        self._query = terms(query)
        frequency = dict.fromkeys(self._query, 0)
        count = 0
        for document in documents:
            count += 1
            for term in self._query & terms(document):
                frequency[term] += 1
        self._weights = {
            term: math.log(1.0 + (count + 1) / (seen + 1))
            for term, seen in frequency.items()
        }
        self._total = 2.0 * sum(self._weights.values())

    def score(self, name: str, description: str = "") -> float:
        """Relevance in ``[0, 1]``; zero when no query term matches."""
        if not self._total:
            return 0.0
        name_terms = terms(name)
        matched = self._query & (name_terms | terms(description))
        weight = sum(
            self._weights[term] * (2.0 if term in name_terms else 1.0)
            for term in matched
        )
        return weight / self._total
