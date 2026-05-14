from cwscraper.core.niche import load_niche
from cwscraper.replies.drafter import draft_reply


def test_lives_alone_template_picked():
    pack = load_niche("caregiver")
    lead = {
        "id": "abc",
        "title": "Worried my mom lives alone now",
        "selftext_preview": "",
        "url": "https://reddit.com/r/x/comments/abc",
        "source": "r/AgingParents",
    }
    draft = draft_reply(lead, pack)
    assert draft["template_used"] == "lives_alone"
    # personalization replaces {parent}/{parent_name}
    assert "mom" in draft["draft_text"].lower()
    assert "{parent}" not in draft["draft_text"]


def test_fallback_to_default():
    pack = load_niche("caregiver")
    lead = {
        "id": "xyz",
        "title": "something totally generic",
        "selftext_preview": "no triggers here",
        "url": "",
        "source": "",
    }
    draft = draft_reply(lead, pack)
    assert draft["template_used"] == pack.default_reply_template
