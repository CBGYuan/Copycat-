"""Is this the same question we already asked?

Extracted from learning_routes so the gap strip and the decision ledger share
ONE definition. They previously needed the same judgement for two symptoms of
the same problem — a "still missing" item restating an open question, and an
open decision the interview had already circled back on and gotten answered —
and two private copies of "roughly the same wording" would have drifted into
disagreeing about the same pair of questions.
"""
import re

from utils import skill_dedup


# A question is "the same" as an earlier one well below verbatim: the model
# rewords its own list every round ("which AP" / "which AP was blacklisted"),
# and exact matching turned every rewording into a brand-new item the engineer
# had already dealt with.
_SAME_RATIO = 0.82
_CONTAINS_MIN = 12      # below this a substring hit is coincidence, not a repeat

# What two questions are ARGUING about: log keywords (ROAM_DECISION_SM),
# numbers (a threshold, a channel), acronyms. "why was ROAM_A excluded" and
# "why was ROAM_B excluded" read as 95% identical to any string metric while
# being entirely different questions, so a mismatch here vetoes the fuzzy
# match.
_SIGNAL_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*|\b[A-Z]{3,}\b|\b\d+(?:\.\d+)?\b")

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_STOPWORDS = {
    "the", "and", "are", "was", "were", "for", "that", "this", "which", "what", "why",
    "how", "does", "did", "been", "with", "from", "not", "any", "all", "its", "their",
    "there", "here", "you", "your", "they", "has", "have", "had", "can", "should",
    "would", "about", "into", "only", "one", "when", "where", "who", "whether",
}


def signal_tokens(text: str) -> set:
    return {t.casefold() for t in _SIGNAL_RE.findall(text or "")}


def content_tokens(text: str) -> set:
    return {t for t in (w.casefold() for w in _WORD_RE.findall(text or ""))
            if len(t) > 2 and t not in _STOPWORDS}


def same_question(a: str, b: str) -> bool:
    """Strict: near-verbatim restatements of each other."""
    na, nb = skill_dedup.normalize(a), skill_dedup.normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Erring towards asking again: a question wrongly shown is a second of the
    # engineer's time, a question wrongly swallowed is knowledge never captured.
    if signal_tokens(a) != signal_tokens(b):
        return False
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= _CONTAINS_MIN and short in long:
        return True
    return skill_dedup.ratio(na, nb) >= _SAME_RATIO


def same_topic(a: str, b: str) -> bool:
    """Looser than same_question, for "is the chat already covering this?".
    The interview rarely echoes the earlier wording, but asking the same thing
    in its own words is still the same piece of work."""
    if same_question(a, b):
        return True
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return False
    # Containment, not Jaccard: a one-line gap against a three-line question is
    # a subset, and dividing by the union would score it as unrelated.
    overlap = len(ta & tb) / min(len(ta), len(tb))
    shares_symbol = bool(signal_tokens(a) & signal_tokens(b))
    return overlap >= 0.6 or (shares_symbol and overlap >= 0.4)


def answer_covers(question: str, text: str) -> bool:
    """Did this piece of teaching actually answer that question?

    `text` is prose the engineer wrote (a step explanation, a chat reply), not
    another question, so the failure mode is different from same_topic's: a
    long answer shares ordinary vocabulary with everything, and scoring it the
    same way would close half the list on one paragraph. So the question's own
    signal — the log keyword, threshold or acronym it is ABOUT — has to appear
    in the text before any wording overlap is believed. A question with no
    signal at all has only its wording to go on and needs much more of it.
    """
    tq = content_tokens(question)
    tt = content_tokens(text)
    if not tq or not tt:
        return False
    overlap = len(tq & tt) / len(tq)
    sq = signal_tokens(question)
    if sq:
        return bool(sq & signal_tokens(text)) and overlap >= 0.5
    return overlap >= 0.75
