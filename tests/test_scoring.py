from cwscraper.core.scoring import classify_intent


def test_high_intent_wins_over_medium():
    intent, matched = classify_intent(
        "my mom lives alone and I need daily check in help",
        high_intent_keywords=["lives alone", "daily check in"],
        medium_intent_keywords=["aging parent"],
    )
    assert intent == "high"
    assert "lives alone" in matched


def test_medium_intent_when_no_high():
    intent, matched = classify_intent(
        "tips for caring for an aging parent",
        high_intent_keywords=["wellness check"],
        medium_intent_keywords=["aging parent"],
    )
    assert intent == "medium"
    assert matched == ["aging parent"]


def test_no_match_returns_none():
    intent, matched = classify_intent(
        "totally unrelated post about gardening",
        high_intent_keywords=["wellness check"],
        medium_intent_keywords=["aging parent"],
    )
    assert intent is None
    assert matched == []


def test_case_insensitive():
    intent, matched = classify_intent(
        "MY DAD LIVES ALONE NOW",
        high_intent_keywords=["lives alone"],
        medium_intent_keywords=[],
    )
    assert intent == "high"
