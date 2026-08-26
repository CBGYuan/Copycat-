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
# Wi-Fi/BT questions are usually ABOUT a camelCase driver symbol or a hex
# value — spdConfigId, completeOidCommon, 0xffdc3a1c, bit6. An earlier version
# only recognised SNAKE_CASE, ALLCAPS and plain integers, so those questions
# had NO signal at all: they fell through to the wording-only branch, and an
# answer naming the exact symbol still failed to close them.
_SIGNAL_RE = re.compile(
    r"0[xX][0-9a-fA-F]+"                        # hex literal: 0x46, 0xffdc3a1c
    r"|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*"      # SNAKE_CASE / mixed_underscore
    r"|\b[A-Z]{3,}\b"                           # ALLCAPS acronym
    r"|\b[A-Za-z]+(?:[A-Z][a-z0-9]+)+\b"        # camelCase / PascalCase symbol
    r"|\b[A-Za-z]{2,}\d+\b"                     # bit6, ch11
    r"|\b\d+(?:\.\d+)?\b"                       # plain number / threshold
)

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


# Below this a shared prefix is a coincidence rather than a message family.
_FAMILY_MIN = 6


def _family_matches(a: set, b: set) -> set:
    """Signals in `a` naming a family that `b` names a member of.

    A question asks about ROAMING_DECISION_STATE; the answer names
    ROAMING_DECISION_STATE_ROAM_DECISION_PRE_TASK_ROAM — the exact state, which
    is a better answer than the question asked for. Token equality read the two
    as unrelated, so answering in full detail failed to close the question.
    """
    wide = [t for t in b if len(t) >= _FAMILY_MIN]
    return {x for x in a - b if len(x) >= _FAMILY_MIN
            and any(y.startswith(x + "_") or x.startswith(y + "_") for y in wide)}


def _symbol_words(tokens: set) -> set:
    """The plain words buried inside compound symbol names.

    PRE_TASK_ROAM is one token, so an answer naming the exact state scored zero
    for "roam", "state" and "task" — naming the symbol precisely LOWERED the
    wording overlap against a question asking about it in prose.
    """
    return {p for t in tokens if "_" in t
            for p in t.split("_") if len(p) > 2 and p not in _STOPWORDS}


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


# A reply this short is being written AS an answer — nobody drops a log
# keyword into a dozen words by accident. Past it, a matching keyword could be
# an aside inside a longer explanation, so wording is checked as well.
_SHORT_ANSWER_TOKENS = 14

# For a question with nothing to anchor on. The high bar is "how much of the
# question did you repeat", which only a restatement clears; the low one is
# "how much of this short reply is about the question", which is the only
# question a two-line answer can be asked. Two shared words minimum, so a
# single overlapping word can't carry it.
_NO_SIGNAL_WORDING = 0.75
_NO_SIGNAL_SHORT_WORDING = 0.4
_NO_SIGNAL_SHORT_SHARED = 2


def answer_covers(question: str, text: str) -> bool:
    """Did this piece of teaching actually answer that question?

    `text` is prose the engineer wrote (a step explanation, a chat reply), not
    another question, so the failure mode is different from same_topic's: a
    long answer shares ordinary vocabulary with everything, and scoring it the
    same way would close half the list on one paragraph. So the question's own
    signal — the log keyword, threshold or acronym it is ABOUT — has to appear
    in the text before any wording overlap is believed.

    The wording bar is NOT scaled by the question's length. A direct answer
    does not echo the question back: "0x46-0x85, BNR 500-32000" answers it
    completely while repeating almost none of its vocabulary. Measuring against
    the QUESTION's token count is a bar only a restatement can clear, which is
    what left short answers sitting in "Still missing" forever. What actually
    needs separating is an answer from a passing mention, and the ANSWER's
    length is what separates those.

    A question with NO signal gets the same treatment one step later. The model
    writes plenty of gaps in plain prose — "enumeration of conditions that
    produce this outcome" carries no symbol at all — and those used to be
    closable only by near-restatement, so a short, precise reply to one of them
    was unmatchable no matter how right it was. A short reply is therefore
    scored on how much of ITSELF lands inside the question instead. Long text
    still faces the restatement bar, because at that length shared vocabulary
    is what any two paragraphs about the same log have in common.
    """
    tq = content_tokens(question)
    tt = content_tokens(text)
    if not tq or not tt:
        return False
    sq = signal_tokens(question)
    if not sq:
        shared_words = tq & tt
        if len(tt) <= _SHORT_ANSWER_TOKENS and len(shared_words) >= _NO_SIGNAL_SHORT_SHARED:
            return len(shared_words) / len(tt) >= _NO_SIGNAL_SHORT_WORDING
        return len(shared_words) / len(tq) >= _NO_SIGNAL_WORDING
    st = signal_tokens(text)
    exact = sq & st
    # A family match is weaker evidence than naming the symbol outright, so it
    # opens the wording check rather than deciding on its own.
    shared = exact | _family_matches(sq, st)
    if not shared:
        return False
    if len(tt) <= _SHORT_ANSWER_TOKENS or len(exact) >= 2:
        return True
    return len(tq & (tt | _symbol_words(tt))) / len(tq) >= 0.5
