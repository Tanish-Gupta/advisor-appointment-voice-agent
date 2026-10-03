"""TopicMapper (LLD 3.4): synonyms, word boundaries, ties and list picks."""

import pytest

from advisor_agent.domain.models import Topic
from advisor_agent.nlu.topic_mapper import TopicMapper, match_topic, pick_from_list

KYC, SIP, TAX, WD, ACC = (Topic.KYC_ONBOARDING, Topic.SIP_MANDATES, Topic.STATEMENTS_TAX,
                          Topic.WITHDRAWALS, Topic.ACCOUNT_CHANGES)  # fmt: skip


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("KYC/Onboarding", KYC),
        ("Withdrawals & Timelines", WD),
        ("my kyc is pending", KYC),
        ("I need to update my aadhaar", KYC),
        ("help with my PAN card", KYC),
        ("I want to open an account", KYC),
        ("my sip mandate failed", SIP),
        ("change the autopay debit date", SIP),
        ("I need my capital gains statement", TAX),
        ("questions about form 16", TAX),
        ("tax docs", TAX),
        ("how do I redeem my funds", WD),
        ("when will I get my money back", WD),
        ("add a nominee", ACC),
        ("update my address", ACC),
        ("I want to change my bank account", ACC),
    ],
)
def test_maps_synonyms(text, topic):
    assert match_topic(text) == (topic, [topic])
    assert TopicMapper().map(text) is topic


@pytest.mark.parametrize("text", ["mortgages", "cash", "Japan", "hello", "the weather"])
def test_no_topic(text):
    assert match_topic(text) == (None, [])


def test_word_boundaries():
    # "cash" must not hit "cas"; "Japan" must not hit "pan"; "gossip" must not hit "sip"
    assert match_topic("I have cash in Japan and like gossip") == (None, [])


def test_tie_is_ambiguous():
    topic, candidates = match_topic("tax on my SIP withdrawal")
    assert topic is None
    assert candidates == [SIP, TAX, WD]  # in ASK_TOPIC order
    assert TopicMapper().candidates("tax on my SIP withdrawal") == [SIP, TAX, WD]


def test_more_hits_wins():
    # SIP has two hits (sip + mandate), tax has one
    assert match_topic("sip mandate tax")[0] is SIP


@pytest.mark.parametrize(
    ("text", "topic"),
    [("1", KYC), ("2", SIP), ("option 3", TAX), ("topic 4", WD), ("number two", SIP),
     ("five", ACC), ("the first one", KYC), ("second", SIP), ("last", ACC)],
)  # fmt: skip
def test_pick_from_list(text, topic):
    assert pick_from_list(text) is topic


@pytest.mark.parametrize("text", ["6", "0", "sip", "first thing tomorrow", ""])
def test_pick_from_list_none(text):
    assert pick_from_list(text) is None
