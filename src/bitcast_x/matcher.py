"""Deterministic lexical pre-claim matcher selected by the shadow experiment."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

_TOKEN = re.compile(r"https?://\S+|@[A-Za-z0-9_]+|#[\w]+|[\w']+", re.UNICODE)
_SPACE = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")
WINNER_FLOOR = 0.70
WINNER_MARGIN = 0.10


def normalize_match_text(text: str) -> str:
    """Apply the frozen NFKC/case/URL/whitespace normalization."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = _URL.sub(" <url> ", normalized)
    return _SPACE.sub(" ", normalized).strip()


def match_tokens(text: str) -> list[str]:
    """Tokenize URLs, mentions, hashtags, and Unicode words."""

    return _TOKEN.findall(normalize_match_text(text))


def _ngrams(text: str, size: int = 3) -> Counter[str]:
    compact = f"  {normalize_match_text(text)}  "
    return Counter(
        compact[index : index + size] for index in range(max(len(compact) - size + 1, 0))
    )


def _dice(left: Counter[str], right: Counter[str]) -> float:
    total = sum(left.values()) + sum(right.values())
    return 2 * sum((left & right).values()) / total if total else 1.0


def lexical_score(draft: str, published: str, public_terms: set[str]) -> float:
    """Return the frozen 55% trigram Dice plus 45% non-public multiset Jaccard."""

    left = Counter(token for token in match_tokens(draft) if token not in public_terms)
    right = Counter(token for token in match_tokens(published) if token not in public_terms)
    union = sum((left | right).values())
    jaccard = sum((left & right).values()) / union if union else 0.0
    return 0.55 * _dice(_ngrams(draft), _ngrams(published)) + 0.45 * jaccard


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """One active claim draft eligible to compete for a published tweet."""

    miner_hotkey: str
    claim_id: str
    draft: str


@dataclass(frozen=True, slots=True)
class MatchDecision:
    """Auditable deterministic winner decision."""

    winner: MatchCandidate | None
    winner_score: float
    runner_up_score: float
    reason: str


def choose_match(
    published: str,
    candidates: list[MatchCandidate],
    *,
    public_text: str,
) -> MatchDecision:
    """Rank candidates by frozen score and tie-break rules, abstaining conservatively."""

    public_terms = set(match_tokens(public_text))
    ranked = sorted(
        (
            (lexical_score(candidate.draft, published, public_terms), candidate)
            for candidate in candidates
        ),
        key=lambda item: (-item[0], item[1].miner_hotkey, item[1].claim_id),
    )
    if not ranked:
        return MatchDecision(None, 0.0, 0.0, "claim_not_active")
    winner_score, winner = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    if len(ranked) > 1 and winner_score == runner_up:
        return MatchDecision(None, winner_score, runner_up, "ambiguous_match")
    if winner_score < WINNER_FLOOR:
        return MatchDecision(None, winner_score, runner_up, "score_below_floor")
    if winner_score - runner_up < WINNER_MARGIN:
        return MatchDecision(None, winner_score, runner_up, "ambiguous_match")
    return MatchDecision(winner, winner_score, runner_up, "accepted")
