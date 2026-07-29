"""
Skill de-duplication — comparing a freshly-taught skill draft against the
skill it was taught on top of (the "parent": a loaded shared/cloud skill).

Ported from ACE's Curator/grow-and-refine (services/ace/playbook.py in
wireless_ce_avatar), with two deliberate adaptations. Both matter enough that
copying ACE's behaviour verbatim would be a bug here, not a feature:

1. NEAR-DUPLICATES ARE FLAGGED, NEVER AUTO-DROPPED.
   ACE can afford a permissive 0.85 similarity threshold because its failure
   mode is benign: a false positive merely merges two bullets' counters, and
   its own comment says as much ("false positives just merge counters").
   Here a false positive would silently delete a keyword an engineer
   deliberately taught, which is destructive and invisible. So the buckets
   are split by CONFIDENCE: exact matches (after normalisation) are safe to
   remove automatically; anything merely similar is surfaced for the
   engineer to decide. This follows the same rule the existing merge already
   commits to — see basic_merge_draft: "never silently discarded".

2. SUBSTRING CONTAINMENT IS A STRONGER SIGNAL THAN TEXT SIMILARITY.
   ACE compares free-text prose bullets, where ratio similarity is the right
   tool. Copycat's keywords are TAT substring matchers, which gives an exact
   redundancy law that beats any fuzzy score: if the parent already filters
   on "DeAuth", then a child's "DeAuth detected" cannot match a single line
   the parent doesn't already match — it is provably redundant, at any
   similarity ratio. The reverse direction is NOT redundant but the opposite:
   a child's "DeAuth" against a parent's "DeAuth detected" is BROADER, and
   silently dropping it would narrow the skill. Those two cases are
   therefore reported separately (`covered` vs `widens`), never lumped into
   one "similar" bucket.

The ACE ideas kept as-is: itemise before comparing (compare rule-by-rule,
not document-to-document — the single biggest reason Copycat's current
whole-text line matching misses reworded duplicates), and difflib's
SequenceMatcher ratio as the cheap, deterministic, no-LLM similarity measure.
"""
import re
from difflib import SequenceMatcher
from typing import Dict, List

# Same value as ACE's playbook.DEDUP_RATIO. Kept identical on purpose so the
# two systems agree about what "near-duplicate" means — but note the
# consequence differs (see this module's docstring): here it only ever routes
# an item into the review bucket, it never deletes anything.
NEAR_RATIO = 0.85

# A rule bullet shorter than this is too small for ratio similarity to mean
# anything ("N/A", "See above") — two unrelated short fragments can easily
# score above 0.85 by accident. Below it, only exact matches count.
_MIN_RATIO_LEN = 25

# Leading list markers on an expert_rules line: "1.", "2)", "-", "*", "•".
_BULLET_PREFIX = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")

# Separates inherited rules from this session's additions inside a child
# skill's expert_rules (see build_extension_skill). Everything ABOVE it came
# from the parent, so a re-export against an updated parent can replace that
# whole region without touching what the engineer added below it.
_INHERIT_MARKER = "# ── Extension:"

# How deep the vertical chain may go: a child's `lineage` may hold at most
# this many ancestors (root + one middle generation). A gen-3 export is
# refused inheritance and falls back to a standalone skill.
#
# The limit is not stylistic — it comes from how Avatar actually consumes a
# skill (verified in wireless_ce_avatar/services/log_chatbot_service.py):
# `fetch_filtered_logs(skill_name)` filters the log with that skill's keyword
# list and hands the matched lines back as "Current Skill Evidence"
# (_build_skill_focus_payload). Because extension exports are FLAT, every
# generation's keyword list is a superset of its parent's — so by gen 3 the
# filter matches most of the log, the payload blows past MAX_SKILL_FOCUS_LINES
# and gets head+tail truncated with the middle dropped. A skill that returns
# "almost everything, minus the middle" has stopped being a focus mechanism
# and is strictly worse than the raw log. Depth has to be capped before that.
MAX_LINEAGE_DEPTH = 2

# Two one-sentence descriptions scoring at or above this are treated as too
# alike to tell apart. Lower than NEAR_RATIO on purpose: this is an advisory
# warning about prose, not a redundancy verdict about a filter token, and the
# cost of a missed warning (an unpickable skill pair) is higher than the cost
# of a spurious one (a line of text the engineer overrules).
DESC_CONFLICT_RATIO = 0.75


def normalize(text: str) -> str:
    """Casefold + collapse all whitespace runs to one space.

    Whitespace normalisation is not cosmetic here: real skills.yaml keywords
    carry meaningful-looking but incidental padding (" ------- RESUME FLOW",
    " ---------- SUSPEND FLOW FINISHED ----------"), and hand-retyping one
    almost never reproduces the exact spacing. Without this, the existing
    case-insensitive exact match reports a genuine duplicate as new.
    """
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def ratio(a: str, b: str) -> float:
    """Similarity in [0,1] between two normalised strings."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def split_rules(expert_rules: str) -> List[str]:
    """Itemise an expert_rules blob into comparable units.

    This is the ACE 'bullets, not a monolith' principle applied to the one
    field where Copycat currently compares whole text: splitting first is
    what lets a single reworded rule be spotted as a near-duplicate instead
    of the entire block being declared new because one line changed.

    Splits on blank lines and on numbered/bulleted line starts, so both
    layouts found in real skills.yaml files itemise correctly. Sub-bullets
    indented under a rule stay attached to that rule — they are conditions
    OF it, and comparing them standalone would be meaningless.
    """
    units: List[str] = []
    current: List[str] = []

    def flush():
        if current:
            joined = "\n".join(current).strip()
            if joined:
                units.append(joined)
            current.clear()

    for raw_line in (expert_rules or "").splitlines():
        if not raw_line.strip():
            flush()
            continue
        # A new top-level numbered/bulleted item starts a new unit, but only
        # when it is NOT indented — an indented "- ..." is a sub-condition.
        is_top_level = raw_line[:1] not in (" ", "\t")
        if is_top_level and _BULLET_PREFIX.match(raw_line) and current:
            flush()
        current.append(raw_line.rstrip())
    flush()
    return units


def _keyword_relation(candidate: str, existing: str) -> str:
    """How `candidate` relates to one `existing` parent keyword.

    Returns "exact" | "covered" | "widens" | "near" | "distinct".
    See the module docstring for why covered/widens are not interchangeable.
    """
    c, e = normalize(candidate), normalize(existing)
    if not c or not e:
        return "distinct"
    if c == e:
        return "exact"
    if e in c:
        # Parent's filter is shorter -> it already matches every line the
        # candidate could match. Candidate contributes nothing.
        return "covered"
    if c in e:
        # Candidate is shorter -> it matches MORE than the parent's. Keeping
        # it genuinely widens the skill; dropping it would narrow coverage.
        return "widens"
    if ratio(candidate, existing) >= NEAR_RATIO:
        return "near"
    return "distinct"


def dedupe_keywords(candidates: List[str], parent: List[str]) -> Dict[str, List[Dict]]:
    """Bucket each candidate keyword against the parent skill's keyword list.

      new       — nothing in the parent resembles it. Safe to add.
      exact     — already in the parent (after normalisation). Safe to drop.
      covered   — a SHORTER parent keyword already matches everything this
                  would; provably redundant for substring filtering.
      widens    — this is shorter/broader than a parent keyword it contains;
                  keeping it expands coverage. Reported so the engineer can
                  see it supersedes the parent's narrower one.
      near      — merely similar. NEVER auto-dropped; needs a human call.

    Each non-`new` entry carries `matched` (the parent keyword responsible)
    so the UI can always show WHY something was bucketed, rather than an
    unexplained disappearance.
    """
    out: Dict[str, List[Dict]] = {"new": [], "exact": [], "covered": [], "widens": [], "near": []}
    for cand in candidates or []:
        cand = str(cand or "").strip()
        if not cand:
            continue
        best_bucket, best_match, best_score = "distinct", None, 0.0
        for ex in parent or []:
            rel = _keyword_relation(cand, ex)
            if rel == "distinct":
                continue
            score = 1.0 if rel == "exact" else ratio(cand, ex)
            # Priority: an exact hit wins outright; otherwise keep the
            # closest match so the reason shown is the most convincing one.
            if rel == "exact":
                best_bucket, best_match, best_score = "exact", ex, 1.0
                break
            if score > best_score:
                best_bucket, best_match, best_score = rel, ex, score
        if best_bucket == "distinct":
            out["new"].append({"text": cand})
        else:
            out[best_bucket].append({
                "text": cand, "matched": best_match, "score": round(best_score, 3),
            })
    return out


def dedupe_rules(candidate_rules: str, parent_rules: str) -> Dict[str, List[Dict]]:
    """Same bucketing for expert_rules, itemised first (see split_rules).

    Only three buckets here — `covered`/`widens` are substring-filter
    concepts that don't apply to prose. A rule that merely restates a
    parent rule in different words lands in `near` and is left for the
    engineer, on the same no-silent-loss principle as keywords.
    """
    parent_units = split_rules(parent_rules)
    out: Dict[str, List[Dict]] = {"new": [], "exact": [], "near": []}
    for unit in split_rules(candidate_rules):
        norm = normalize(unit)
        best_match, best_score = None, 0.0
        matched_exact = False
        for pu in parent_units:
            if normalize(pu) == norm:
                matched_exact, best_match, best_score = True, pu, 1.0
                break
            # Ratio on very short fragments is noise, not signal.
            if len(norm) < _MIN_RATIO_LEN:
                continue
            s = ratio(unit, pu)
            if s > best_score:
                best_match, best_score = pu, s
        if matched_exact:
            out["exact"].append({"text": unit, "matched": best_match, "score": 1.0})
        elif best_score >= NEAR_RATIO:
            out["near"].append({"text": unit, "matched": best_match, "score": round(best_score, 3)})
        else:
            out["new"].append({"text": unit})
    return out


def _keep_texts(bucket: Dict[str, List[Dict]], keys: List[str]) -> List[str]:
    return [item["text"] for k in keys for item in bucket.get(k, [])]


def build_deduped_skill(draft: Dict, diff: Dict, keep_near: bool = True) -> Dict:
    """BUTTON 1 — a standalone skill holding ONLY what the parent doesn't
    already have. Provably-redundant content (exact / covered) is dropped;
    everything else is kept.

    `keep_near` defaults to True and should stay that way unless the engineer
    has explicitly reviewed the near-matches: those are the ones the
    similarity score merely *suspects*, and dropping an unreviewed suspicion
    is how a taught keyword disappears without anyone noticing. `widens` is
    always kept — a broader keyword is the opposite of redundant.

    The result is deliberately NOT self-sufficient: it assumes the parent
    skill is also loaded. Use build_extension_skill when the exported entry
    has to stand on its own.
    """
    keep = ["new", "widens"] + (["near"] if keep_near else [])
    return {
        **draft,
        "keywords": _keep_texts(diff["keywords"], keep),
        "exclusive": _keep_texts(diff["exclusive"], keep),
        "expert_rules": "\n\n".join(
            _keep_texts(diff["expert_rules"], ["new"] + (["near"] if keep_near else []))
        ),
    }


def build_extension_skill(draft: Dict, parent, parent_key: str, diff: Dict,
                          keep_near: bool = True) -> Dict:
    """BUTTON 2 — a skill built on the parent's framework, carrying the whole
    inherited body PLUS this session's additions, and recording the ancestry.

    Why fully resolved rather than delta-only: Avatar's loader reads only
    name/description/keywords/exclusive/expert_rules and silently ignores
    anything else, so a child that stored just its delta and pointed at a
    `parent` key would load there with a fraction of its keywords and analyse
    logs quietly wrongly. The inheritance is therefore expressed as
    STRUCTURE, not as a reference to resolve at load time:

      - keywords/exclusive: parent's first (order preserved, so the chain is
        readable), then only the genuinely-new ones appended.
      - expert_rules: the parent's rules verbatim, then a labelled section
        holding just what this session added — so a reader can see at a
        glance which knowledge is inherited and which is new, and a future
        re-export can regenerate the child from an updated parent by
        replacing everything above that marker.
      - parent/lineage: the vertical chain itself, root→…→parent→this.

    Nothing from the parent is ever dropped or rewritten here, which is what
    makes repeated re-exports safe.
    """
    parent_kw = list(getattr(parent, "keywords", []) or [])
    parent_ex = list(getattr(parent, "exclusive", []) or [])
    parent_rules = (getattr(parent, "expert_rules", "") or "").strip()
    parent_name = getattr(parent, "name", parent_key)

    keep = ["new", "widens"] + (["near"] if keep_near else [])
    new_kw = _keep_texts(diff["keywords"], keep)
    new_ex = _keep_texts(diff["exclusive"], keep)
    new_rules = _keep_texts(diff["expert_rules"], ["new"] + (["near"] if keep_near else []))

    child_name = draft.get("name") or "Extended skill"
    sections = []
    if parent_rules:
        sections.append(parent_rules)
    if new_rules:
        sections.append(
            f"{_INHERIT_MARKER} added by \"{child_name}\" on top of \"{parent_name}\" —\n"
            + "\n\n".join(new_rules)
        )

    ancestry = list(getattr(parent, "lineage", []) or []) + [parent_key]
    return {
        **draft,
        "keywords": parent_kw + new_kw,
        "exclusive": parent_ex + new_ex,
        "expert_rules": "\n\n".join(sections),
        "parent": parent_key,
        "lineage": ancestry,
        # What the UI needs to explain the result without recomputing it.
        "inherited_counts": {
            "keywords": len(parent_kw), "exclusive": len(parent_ex),
            "rules": len(split_rules(parent_rules)),
        },
        "added_counts": {
            "keywords": len(new_kw), "exclusive": len(new_ex), "rules": len(new_rules),
        },
    }


def lineage_depth_check(parent) -> Dict:
    """Can a new child be hung off `parent` without exceeding MAX_LINEAGE_DEPTH?

    Returns {allowed, child_depth, max_depth}. `child_depth` is what the new
    skill's `lineage` WOULD be: the parent's own ancestry plus the parent.

    Refusing inheritance must never mean refusing the export — the engineer's
    taught knowledge is not the thing at fault. The caller falls back to the
    standalone path (see learning_routes.converge), so the skill is still
    created; it just starts a fresh root instead of deepening a chain that has
    already reached the point where Avatar's filter stops discriminating.
    """
    depth = len(list(getattr(parent, "lineage", []) or [])) + 1
    return {"allowed": depth <= MAX_LINEAGE_DEPTH,
            "child_depth": depth, "max_depth": MAX_LINEAGE_DEPTH}


def description_conflict(child_description: str, parent) -> Dict:
    """Is the child's one-line description too close to its parent's to pick
    between? Returns {too_similar, score, parent_description}.

    This guards the ONE thing a flat extension cannot express structurally.
    Avatar's agent chooses a skill purely from the `name: description` lines in
    its system prompt (_build_analyze_system_prompt) and can only pass a key
    from the `enum` of skill names (_build_tools) — it never sees keywords or
    expert_rules at selection time. A child inherits all of its parent's
    keywords, so the description is the entire basis on which the two can be
    told apart. Two skills that read the same are not a cosmetic problem: the
    agent picks arbitrarily, and picking the child returns the parent's log
    lines too.

    Advisory only. It is a `difflib` ratio, so it catches the blatant failure
    (the child's description is a reworded copy) and says nothing about two
    differently-worded descriptions that happen to overlap in meaning — which
    is why the prompt is also told to keep them exclusive, rather than relying
    on this check alone.
    """
    parent_desc = (getattr(parent, "description", "") or "").strip()
    child_desc = (child_description or "").strip()
    if not parent_desc or not child_desc:
        return {"too_similar": False, "score": 0.0, "parent_description": parent_desc}
    score = ratio(child_desc, parent_desc)
    return {"too_similar": score >= DESC_CONFLICT_RATIO,
            "score": round(score, 3), "parent_description": parent_desc}


def diff_against_parent(draft: Dict, parent) -> Dict:
    """Full comparison of a skill draft against its parent skill.

    `parent` is a services.skill_service.Skill (or anything exposing
    .keywords/.exclusive/.expert_rules). Returns the three per-field
    bucketings plus a `summary` of how much of the draft is genuinely new —
    which is what the Export UI needs to tell the engineer "8 of your 11
    keywords are already in the cloud skill" BEFORE anything is removed.
    """
    kw = dedupe_keywords(draft.get("keywords") or [], list(getattr(parent, "keywords", []) or []))
    ex = dedupe_keywords(draft.get("exclusive") or [], list(getattr(parent, "exclusive", []) or []))
    rules = dedupe_rules(draft.get("expert_rules") or "", getattr(parent, "expert_rules", "") or "")

    def _counts(b: Dict[str, List[Dict]]) -> Dict[str, int]:
        return {k: len(v) for k, v in b.items()}

    return {
        "keywords": kw,
        "exclusive": ex,
        "expert_rules": rules,
        "summary": {
            "keywords": _counts(kw),
            "exclusive": _counts(ex),
            "expert_rules": _counts(rules),
            # Auto-removable = provably redundant only. `near` is excluded by
            # design: it is exactly the set a human still has to rule on.
            "auto_removable": (
                len(kw["exact"]) + len(kw["covered"])
                + len(ex["exact"]) + len(ex["covered"])
                + len(rules["exact"])
            ),
            "needs_review": len(kw["near"]) + len(ex["near"]) + len(rules["near"]),
        },
    }
