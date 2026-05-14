from cwscraper.core.niche import load_niche


def test_caregiver_pack_loads():
    pack = load_niche("caregiver")
    assert pack.slug == "caregiver"
    assert pack.display_name
    assert pack.high_intent_keywords
    assert pack.medium_intent_keywords
    assert pack.subreddits
    assert pack.reply_templates
    # ensure default reply template is present
    assert pack.reply_template(pack.default_reply_template) is not None


def test_blank_pack_loads():
    pack = load_niche("blank")
    assert pack.slug == "blank"
    assert pack.high_intent_keywords == []
    assert pack.reply_templates  # at least the fallback template


def test_keywords_are_lowercased():
    pack = load_niche("caregiver")
    for kw in pack.high_intent_keywords + pack.medium_intent_keywords:
        assert kw == kw.lower()
