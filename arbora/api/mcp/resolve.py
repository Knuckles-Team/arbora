"""Intent ranking over already-authorized operation and fleet descriptors.

The caller filters descriptors through scope, principal and Eunomia policy before
passing them here. A ranking can select an operation, but it cannot authorize it.
Only a trusted, completed invocation may feed :meth:`record_execution`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from collections import Counter, OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

_WORDS = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the of for to in on with and or is are be do what how why my me i you your please".split()
    # ARBORA-FLEET-R014.2: request verbs say how to answer, not what to run.
    + "show display tell give across".split()
    # ARBORA-FLEET-R014.3: question and function words name no operation.
    + "which who whom whose where when there that this these those it its as at by"
    " from into no any can could should would does did has have".split()
)
_READ_VERBS = frozenset({"ask", "why", "find"})
_VERBS = _READ_VERBS | {"write", "act", "manage"}
#: Catalog, glossary, stewardship, lineage, prompt-candidate and virtual-graph
#: source questions. The engine models these as graph entities
#: (EG-TYPED-PACKS-R106/R108, EG-DECISION-ENGINE-R814, EG-FEDERATED-QUERY-R722)
#: and answers them through its query planner (ARBORA-OPS-R855.4).
_GRAPH_QUESTION = re.compile(
    r"\b(catalog|glossary|steward\w*|lineage|upstream|downstream|virtual[ -]graph"
    r"|who owns|owner of|(connector|data) sources?|prompt candidates?"
    r"|system[ -]prompt candidates?)\b"
)
_GRAPH_OPS = ("query.nl", "query.uql")
#: The share of a plain question's content terms an op must match to answer it
#: instead of the engine planner (ARBORA-FLEET-R014.3).
_PLAIN_QUESTION_COVERAGE = 0.5
_CACHE_LIMIT = 256
_OUTCOME_LIMIT = 4096
_REWARD_WEIGHT = 0.2
_SEMANTIC_WEIGHT = 0.35
_MIN_EVIDENCE = 0.05


def _tokens(value: str) -> Counter[str]:
    return Counter(word for word in _WORDS.findall(value.lower()) if word not in _STOP)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _verb(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _first_str(item: Mapping[str, Any], *keys: str, default: str = "") -> str:
    """The first truthy value among ``keys``, read from an untrusted mapping."""

    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return default


def _str_tuple(item: Mapping[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(value) for value in item.get(key, ()))


@dataclass(frozen=True)
class Descriptor:
    """A generated op descriptor or one visible fleet catalog item."""

    id: str
    verb: str
    summary: str
    examples: tuple[str, ...] = ()
    domain: str = ""
    tags: tuple[str, ...] = ()
    kind: str = "op"
    params_schema: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> Descriptor:
        """Accept registry descriptors and fleet catalog projections uniformly."""
        identifier = _first_str(item, "op", "id")
        if not identifier:
            raise ValueError("A routing descriptor requires an op or id")
        return cls(
            id=identifier,
            verb=_verb(item.get("verb", "act")),
            summary=_first_str(item, "summary", "description"),
            examples=_str_tuple(item, "examples"),
            domain=_first_str(item, "domain"),
            tags=_str_tuple(item, "tags"),
            kind=_first_str(item, "kind", default="op"),
            params_schema=item.get("params_schema") or {},
        )


@dataclass(frozen=True)
class Candidate:
    descriptor: Descriptor
    score: float
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    """Routing decision; ``preview`` requires the invocation layer's plan lease."""

    op: str | None
    params: Mapping[str, Any]
    preview: bool
    missing_required: tuple[str, ...]
    alternatives: tuple[str, ...]
    why: str
    fallback: bool = False


class IntentResolver:
    """Bounded lexical/semantic router with policy-partitioned outcome learning."""

    def __init__(self, *, cache_limit: int = _CACHE_LIMIT) -> None:
        if cache_limit < 1:
            raise ValueError("cache_limit must be positive")
        self._secret = secrets.token_bytes(32)
        self._cache_limit = cache_limit
        self._cache: OrderedDict[tuple[str, ...], tuple[Candidate, ...]] = OrderedDict()
        self._outcomes: OrderedDict[tuple[str, str, str], float] = OrderedDict()
        self._epoch = 0

    def scope_ref(
        self,
        tenant: str,
        policy_revision: str,
        *,
        audience: str = "",
        scopes: Sequence[str] = (),
    ) -> str:
        """Opaque process-local reference for verified authority, never caller hints."""
        if not tenant or not policy_revision:
            raise ValueError("Verified tenant and policy revision are required")
        authority = json.dumps(
            [tenant, policy_revision, audience, sorted(scopes)], separators=(",", ":")
        ).encode()
        return hmac.digest(self._secret, authority, "sha256").hex()

    def rank(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        *,
        scope_ref: str | None,
        semantic_scores: Mapping[str, float] | None = None,
        top_k: int = 5,
    ) -> tuple[Candidate, ...]:
        """Rank only the visible input snapshot; unseen items never enter cache."""
        if verb not in _VERBS:
            raise ValueError(f"Unknown intent verb: {verb}")
        visible = tuple(_coerce(item) for item in descriptors)
        scores = semantic_scores or {}
        limit = max(1, min(top_k, 20))
        key = (
            verb,
            _digest(" ".join(intent.lower().split())),
            _digest([_fingerprint(item) for item in visible]),
            _digest(scores),
            scope_ref or "unverified",
            str(limit),
            str(self._epoch),
        )
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        terms = _tokens(intent)
        pool = tuple(item for item in visible if verb == "find" or item.verb == verb)
        ranked = _ranked_candidates(
            pool, lambda item: self._score(item, terms, scores, scope_ref), limit
        )
        self._cache[key] = ranked
        self._cache.move_to_end(key)
        _evict_oldest(self._cache, self._cache_limit)
        return ranked

    def resolve(
        self,
        verb: str,
        intent: str,
        descriptors: Sequence[Descriptor | Mapping[str, Any]],
        *,
        scope_ref: str | None,
        params: Mapping[str, Any] | None = None,
        semantic_scores: Mapping[str, float] | None = None,
    ) -> Resolution:
        """Select an NL op; mutations always require a reviewed preview first."""
        ranked = self.rank(
            verb,
            intent,
            descriptors,
            scope_ref=scope_ref,
            semantic_scores=semantic_scores,
        )
        supplied = dict(params or {})
        graph_question = bool(_GRAPH_QUESTION.search(intent.lower()))
        if verb == "find":
            ids = tuple(candidate.descriptor.id for candidate in ranked)
            if graph_question:
                visible = {_coerce(item).id for item in descriptors}
                first = tuple(op for op in _GRAPH_OPS if op in visible)
                ids = (*first, *(i for i in ids if i not in first))
            return Resolution(
                None,
                supplied,
                False,
                (),
                ids,
                "Discovery results; no operation executed",
            )
        if verb == "ask" and graph_question and "text" not in supplied:
            routed = _graph_question_route(intent, supplied, descriptors)
            if routed is not None:
                return routed
        if verb == "ask" and not supplied and _nl_visible(descriptors):
            answering = _plain_question_answers(ranked, intent)
            if not answering:
                return replace(
                    _ask_fallback(verb, intent, supplied, descriptors),
                    alternatives=tuple(c.descriptor.id for c in ranked),
                )
            ranked = answering
        winner = ranked[0] if ranked else None
        if winner is None or winner.score < _MIN_EVIDENCE:
            return _ask_fallback(verb, intent, supplied, descriptors)
        if verb == "ask" and _missing_required(
            winner.descriptor.params_schema, supplied
        ):
            # ARBORA-FLEET-R014.1: a plain question cannot fill a required
            # parameter, so an op that needs one does not win `ask`. The next
            # runnable match does; with none, the NL planner answers.
            runnable = next(
                (
                    candidate
                    for candidate in ranked
                    if candidate.score >= _MIN_EVIDENCE
                    and not _missing_required(
                        candidate.descriptor.params_schema, supplied
                    )
                ),
                None,
            )
            if runnable is None:
                fallback = _ask_fallback(verb, intent, supplied, descriptors)
                if fallback.op is not None:
                    return replace(
                        fallback,
                        alternatives=tuple(c.descriptor.id for c in ranked),
                    )
            else:
                winner = runnable
        selected = winner.descriptor
        missing = _missing_required(selected.params_schema, supplied)
        return Resolution(
            op=selected.id,
            params=supplied,
            preview=verb not in _READ_VERBS,
            missing_required=missing,
            alternatives=tuple(
                candidate.descriptor.id
                for candidate in ranked
                if candidate.descriptor.id != selected.id
            ),
            why=f"Matched {', '.join(winner.matched_terms) or 'semantic similarity'}",
        )

    def record_execution(
        self, *, scope_ref: str | None, verb: str, op: str, success: bool
    ) -> None:
        """Record an observed authorized NL dispatch, called by invoke after completion."""
        if scope_ref is None or verb not in _VERBS or not op:
            return
        key = (scope_ref, verb, op)
        previous = self._outcomes.get(key, 0.5)
        self._outcomes[key] = 0.2 * float(success) + 0.8 * previous
        self._outcomes.move_to_end(key)
        _evict_oldest(self._outcomes, _OUTCOME_LIMIT)
        self._epoch += 1

    def _score(
        self,
        item: Descriptor,
        terms: Counter[str],
        semantic_scores: Mapping[str, float],
        scope_ref: str | None,
    ) -> Candidate:
        name = set(_WORDS.findall(item.id.lower()))
        description = _tokens(
            " ".join((item.summary, *item.examples, item.domain, *item.tags))
        )
        matched = tuple(
            sorted(term for term in terms if term in name or term in description)
        )
        weight = sum(
            count * (2 if term in name else 1)
            for term, count in terms.items()
            if term in matched
        )
        lexical = weight / max(sum(terms.values()), 1)
        name_hits = len(name.intersection(matched))
        lexical += (name_hits / len(name)) * 0.01 if name else 0.0
        semantic = float(semantic_scores.get(item.id, 0.0))
        semantic = max(0.0, min(1.0, semantic)) if math.isfinite(semantic) else 0.0
        reward = (
            self._outcomes.get((scope_ref, item.verb, item.id), 0.5)
            if scope_ref
            else 0.5
        )
        return Candidate(
            item,
            lexical + _SEMANTIC_WEIGHT * semantic + _REWARD_WEIGHT * (reward - 0.5),
            matched,
        )


def _coerce(item: Descriptor | Mapping[str, Any]) -> Descriptor:
    return item if isinstance(item, Descriptor) else Descriptor.from_mapping(item)


def _ranked_candidates(
    pool: Sequence[Descriptor],
    score: Any,
    limit: int,
) -> tuple[Candidate, ...]:
    """Score, then keep the top ``limit`` candidates in rank order."""

    return tuple(
        sorted(
            (score(item) for item in pool),
            key=lambda candidate: (-candidate.score, candidate.descriptor.id),
        )[:limit]
    )


def _evict_oldest(cache: OrderedDict[Any, Any], limit: int) -> None:
    while len(cache) > limit:
        cache.popitem(last=False)


def _fingerprint(item: Descriptor) -> tuple[object, ...]:
    return (
        item.id,
        item.verb,
        item.summary,
        item.examples,
        item.domain,
        item.tags,
        item.kind,
        item.params_schema,
    )


def _missing_required(
    schema: Mapping[str, Any], params: Mapping[str, Any]
) -> tuple[str, ...]:
    required = schema.get("required", ())
    return tuple(sorted(str(name) for name in required if name not in params))


def _nl_visible(descriptors: Sequence[Descriptor | Mapping[str, Any]]) -> bool:
    return any(
        item.id == "query.nl" and item.verb == "ask"
        for item in (_coerce(entry) for entry in descriptors)
    )


def _engine_method(item: Descriptor) -> bool:
    """A generated engine method op: structured params, never a text answer."""
    return item.domain == "eg" or item.id.startswith("eg.")


def _plain_question_answers(
    ranked: Sequence[Candidate], intent: str
) -> tuple[Candidate, ...]:
    """The ranked ops that may answer a plain question, in rank order.

    ARBORA-FLEET-R014.3: a question with no parameters is a data, catalog,
    lineage or ownership question for the engine planner unless an op names
    it. A generated engine method needs structured parameters, so it never
    answers one. Another op answers only when it matches at least half of the
    question's content terms; one shared word ("agents", "request") is not
    evidence.
    """
    terms = set(_tokens(intent))
    if not terms:
        return ()
    return tuple(
        candidate
        for candidate in ranked
        if not _engine_method(candidate.descriptor)
        and len(set(candidate.matched_terms)) >= _PLAIN_QUESTION_COVERAGE * len(terms)
    )


def _graph_question_route(
    intent: str,
    params: Mapping[str, Any],
    descriptors: Sequence[Descriptor | Mapping[str, Any]],
) -> Resolution | None:
    """Route a catalog-class question to the engine planner, ahead of ranking.

    Only the authorized ``query.nl`` the caller can see qualifies, so scope and
    policy filtering already happened. The text goes through unchanged and the
    engine binds tenant, row caps and provenance (ARBORA-OPS-R855.4).
    """
    visible = tuple(_coerce(item) for item in descriptors)
    if not any(item.id == "query.nl" and item.verb == "ask" for item in visible):
        return None
    return Resolution(
        "query.nl",
        {**params, "text": intent},
        False,
        (),
        tuple(i.id for i in visible if i.id == "query.uql"),
        "Catalog, lineage or virtual-graph question: answered by the engine planner over its ontology",
    )


def _ask_fallback(
    verb: str,
    intent: str,
    params: Mapping[str, Any],
    descriptors: Sequence[Descriptor | Mapping[str, Any]],
) -> Resolution:
    visible = tuple(_coerce(item) for item in descriptors)
    has_nl = any(item.id == "query.nl" and item.verb == "ask" for item in visible)
    if verb != "ask" or not has_nl:
        return Resolution(
            None, params, False, (), (), "No matching authorized operation"
        )
    return Resolution(
        "query.nl",
        {**params, "text": intent},
        False,
        (),
        (),
        "Using the authorized engine natural-language planner",
        True,
    )
