"""Golden behavior for deterministic matcher v1."""

from itertools import permutations

from bitcast_x.matcher import (
    MatchCandidate,
    choose_match,
    lexical_score,
    match_tokens,
    normalize_match_text,
)


def candidate(hotkey: str, claim_id: str, draft: str) -> MatchCandidate:
    return MatchCandidate(miner_hotkey=hotkey, claim_id=claim_id, draft=draft)


def test_ordinary_edits_keep_genuine_draft_above_adversarial_guess() -> None:
    published = (
        "I have spent a week testing the new wallet. Fast confirmations are useful; "
        "the recovery flow is what finally won me over. https://real.example #Launch"
    )
    genuine = candidate(
        "5" + "a" * 47,
        "01" * 16,
        "I spent a week testing the new wallet — fast confirmations help, but the recovery "
        "flow is what won me over. https://draft.example #Launch",
    )
    guess = candidate(
        "5" + "b" * 47,
        "02" * 16,
        "Big news about the new wallet. Better products and real momentum. #Launch",
    )

    decision = choose_match(
        published,
        [guess, genuine],
        public_text="Talk about the new wallet. Required: #Launch",
    )

    assert decision.winner == genuine
    assert decision.reason == "accepted"
    assert decision.winner_score > decision.runner_up_score + 0.10


def test_public_brief_tokens_do_not_prove_private_draft_access() -> None:
    public = set(match_tokens("Talk about ZipCode and use #ZipCode"))

    score = lexical_score(
        "ZipCode #ZipCode",
        "ZipCode #ZipCode",
        public,
    )

    assert score == 0.55


def test_exact_tie_abstains_despite_deterministic_sort_order() -> None:
    left = candidate("5" + "a" * 47, "01" * 16, "private identical sentence")
    right = candidate("5" + "b" * 47, "02" * 16, "private identical sentence")

    decision = choose_match(
        "private identical sentence",
        [right, left],
        public_text="",
    )

    assert decision.winner is None
    assert decision.reason == "ambiguous_match"


def test_weak_and_close_matches_abstain_with_distinct_reasons() -> None:
    weak = choose_match(
        "completely unrelated published words",
        [candidate("5" + "a" * 47, "01" * 16, "a private draft about something else")],
        public_text="",
    )
    close = choose_match(
        "the same long private wording appears here with one ending",
        [
            candidate(
                "5" + "a" * 47,
                "01" * 16,
                "the same long private wording appears here with first ending",
            ),
            candidate(
                "5" + "b" * 47,
                "02" * 16,
                "the same long private wording appears here with other ending",
            ),
        ],
        public_text="",
    )

    assert weak.reason == "score_below_floor"
    assert close.reason == "ambiguous_match"


def test_candidate_order_property_cannot_change_match_decision() -> None:
    candidates = [
        candidate("5" + "a" * 47, "01" * 16, "private launch analysis with recovery detail"),
        candidate("5" + "b" * 47, "02" * 16, "generic launch announcement"),
        candidate("5" + "c" * 47, "03" * 16, "another unrelated campaign draft"),
    ]
    decisions = {
        choose_match(
            "private launch analysis with recovery detail and one edit",
            list(order),
            public_text="launch",
        )
        for order in permutations(candidates)
    }

    assert len(decisions) == 1


def test_lexical_score_symmetry_and_bounds_properties() -> None:
    corpus = (
        "plain ascii words",
        "Ｆｕｌｌｗｉｄｔｈ Unicode and #Launch",
        "URL https://example.com/a and @Creator",
        "repeated repeated repeated tokens",
        "emoji 🚀 mixed with Ελληνικά",
    )
    for left in corpus:
        for right in corpus:
            forward = lexical_score(left, right, {"and"})
            reverse = lexical_score(right, left, {"and"})
            assert 0.0 <= forward <= 1.0
            assert forward == reverse


def test_normalization_idempotence_property() -> None:
    values = (
        "  Mixed   CASE  ",
        "Ｆｕｌｌｗｉｄｔｈ",
        "Visit https://first.example/x then https://second.example/y",
        "line one\n\tline two",
    )

    for value in values:
        normalized = normalize_match_text(value)
        assert normalize_match_text(normalized) == normalized
