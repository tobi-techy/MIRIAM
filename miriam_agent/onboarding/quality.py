"""Deterministic spec linter for Miriam's onboarding replies.

The personality spec is written as prose; a behavioral eval turns its rules
into checks that run offline on recorded replies. A rule violation is a code,
not a vibe -- so a drift in Miriam's voice fails a test.

Rules map 1:1 to spec sections:

  R1 -- spec §14 short, paragraph-bounded replies (120 words conversational,
        220 for the plan presentation; no more than 4 paragraphs)
  R2 -- spec §15/§33 one question at a time (at most one "?")
  R3 -- spec §16 no generic praise / generic reassurance
  R4 -- spec §17 no one-size-fits-all generic advice
  R5 -- spec §8 talk about BEHAVIOR, never character (no identity attacks)
  R6 -- spec §35 no corporate boilerplate
  R7 -- tapping rule: taps imply a short single question (<= 60 chars, ends "?")
  R8 -- spec §22 never name a money script to the user
  R9 -- no bullet lists in conversational replies (plan presentations may
        enumerate moves)
  R10 -- numbers must be grounded (spec §30, "never invent numbers"): every
        numeric token in a reply must appear in the grounding context
        (``EvalMeta.grounded``), normalized (1,500 == 1500.00 == 1500; a
        percent sign is a ratio, so 60% == 0.6). A reply
        that reports a figure nobody said -- the classic injected/fabricated
        number -- is a hard safety violation. ``grounded`` empty means the rule
        is not scored (there is nothing to ground against).
  R11 -- no parroting (spec v1.1 §6, §53): a reply must not lift a long clause
        straight out of the previous user message ("quite a lot, and you don't
        want to go broke") and then append only a low-information,
        therapist-style tail ("what's making that feel real right now?") that
        adds no new information. Scored only when ``EvalMeta.prev_user`` holds
        the previous user turn; without it nothing can be parroted, so the rule
        stays silent. A mirror-plus-concrete-probe ("okay. but what does
        'going broke' actually look like for you?") is a compliant echo, not a
        parrot -- it substitutes real information for the empty tail.
  R12 -- no therapist mode (spec v1.1 §9): a question that pushes today's
        emotional processing back onto the user ("how does that make you feel?",
        "what's coming up for you?", "what is making that feel real right
        now?") instead of adding information. Conversation is where the
        conflict *shows up*; the emotion is met by naming the pattern plainly,
        never by inviting a therapy session ("what you did there is the
        anxiety doing your banking").

``evaluate_reply`` returns the sorted list of violated rules for one reply;
``[]`` means the reply conforms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_WORDS_CONVERSATIONAL = 120
MAX_WORDS_PRESENT = 220
MAX_PARAGRAPHS = 4
MAX_QUESTION_MARKS = 1
TAP_REPLY_MAX_CHARS = 60

# spec §16: generic praise and reassurance lines.
_GENERIC_PRAISE = (
    "great question",
    "good question",
    "great point",
    "good point",
    "love that",
    "i hear you",
    "i understand how you feel",
    "thanks for sharing",
    "great to hear",
    "that's amazing",
    "that is amazing",
    "awesome",
    "i feel you",
    "no worries at all",
)

# spec §17: generic one-size-fits-all advice frames. Specific advice earned
# from their words ("fund the family envelope first") is fine; these frames say
# *anyone* should do *this* thing.
_GENERIC_ADVICE = (
    "you should budget",
    "you should save",
    "you need to save",
    "you need to",
    "you should",
    "just budget",
    "just save",
    "just track",
    "stop spending",
    "cut back",
    "spend less",
    "save more",
    "make a budget",
    "write it down",
    "start tracking",
)

# spec §8: framing the person, not the behavior.
_IDENTITY_ATTACKS = (
    "you're careless",
    "you are careless",
    "you're irresponsible",
    "you are irresponsible",
    "you're bad with money",
    "you are bad with money",
    "you're lazy",
    "you are lazy",
    "you're undisciplined",
    "you are undisciplined",
    "you're weak",
    "you are weak",
)

# spec §35: corporate/airship boilerplate.
_CORPORATE_BOILERPLATE = (
    "we value",
    "we are committed",
    "we're committed",
    "as a valued customer",
    "please don't hesitate",
    "thank you for your business",
    "best regards",
    "business hours",
    "click here",
    "learn more",
    "our team",
    "we're excited",
    "we are excited",
)

# spec §22: script labels must never reach the user.
_MONEY_SCRIPT_LABELS = (
    "scarcity",
    "income fantasy",
    "lifestyle creep",
    "overcontrol",
    "avoidance",
    "social comparison",
    "status buying",
    "status purchase",
    "money script",
    "family pressure",
)

_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)

# spec v1.1 §9: therapist-mode openers -- emotional processing handed back to
# the user as a question, instead of a concrete probe. "You feel anxious" is
# fine; "how does that make you feel?" is a session, not a feature.
_THERAPIST_PHRASES = (
    "how does that make you feel",
    "how did that make you feel",
    "how does it make you feel",
    "how did it make you feel",
    "what's coming up for you",
    "what is coming up for you",
    "what's coming up when",
    "what are you feeling",
    "how are you feeling",
    "what's going through your head",
    "what is going through your head",
    "walk me through how",
    "walk me through that",
    "tell me more about that",
    "tell me a bit more about that",
    "tell me more about how",
    "what is making that feel real",
    "what's making that feel real",
    "what makes that feel real",
    "i understand how difficult that must be",
    "it sounds like you're feeling",
    "i hear that you're feeling",
    "what would you like to get off your chest",
    "what's the feeling behind",
    "when that happens, what",
    "describe how that feels",
    "could you tell me more",
    "what usually throws it off",
)

# spec v1.1 §6 parroting: tokens that carry no information in either the user's
# clause or Miriam's tail. Split into stopwords (removed everywhere) and
# low-information probe words (removed from the *tail* when deciding whether a
# mirror added anything new). "broke", "control", "money" deliberately stay out
# so a genuine push toward the concrete still counts as new information.
_STOPWORDS = frozenset(
    (
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "so",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "into",
        "onto",
        "off",
        "over",
        "under",
        "out",
        "about",
        "than",
        "then",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "it's",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "i",
        "you",
        "we",
        "they",
        "he",
        "she",
        "me",
        "my",
        "your",
        "our",
        "their",
        "him",
        "her",
        "us",
        "them",
        "you're",
        "i'm",
        "we're",
        "they're",
        "don't",
        "dont",
        "doesn't",
        "didn't",
        "can't",
        "cant",
        "won't",
        "wont",
        "would",
        "should",
        "could",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "not",
        "just",
        "really",
        "very",
        "also",
        "like",
        "go",
        "goes",
        "went",
        "get",
        "gets",
        "got",
        "make",
        "makes",
        "want",
        "wants",
        "need",
        "needs",
        "say",
        "said",
        "know",
        "knows",
        "guess",
        "suppose",
        "seems",
        "seem",
        "sounds",
        "yes",
        "yeah",
        "yep",
        "ok",
        "okay",
        "oh",
        "uh",
        "um",
        "well",
        "right",
        "now",
        "new",
        "actually",
        "honestly",
        "basically",
        "kinda",
        "sorta",
        "hmm",
        "mean",
        "saying",
        "going",
        "coming",
        "thing",
        "things",
        "one",
        "two",
        "something",
        "anything",
        "everything",
        "nothing",
        "some",
        "any",
        "all",
        "both",
        "each",
        "every",
        "more",
        "most",
        "less",
        "too",
        "pretty",
        "maybe",
        "almost",
        "always",
        "never",
        "what",
        "when",
        "where",
        "how",
        "why",
        "who",
        "which",
        "whose",
        "whom",
        "does",
        "did",
    )
)

# Low-information tail vocabulary: the therapist/filler words that make a
# "what's X feel like right now" tail information-dense to the human but empty
# of new content. Any tail word outside (stopwords ∪ user words ∪ this set) is
# genuinely new information, so a mirror plus any real probe survives R11.
_PROCESS_WORDS = frozenset(
    (
        "feel",
        "feeling",
        "felt",
        "feelings",
        "emotion",
        "emotions",
        "emotional",
        "fear",
        "fears",
        "scared",
        "afraid",
        "share",
        "sharing",
        "talk",
        "talking",
        "about",
        "process",
        "journey",
        "space",
        "safe",
        "open",
        "openly",
        "difficult",
        "hard",
        "tough",
        "must",
        "understand",
        "understandable",
        "here",
        "there",
        "with",
        "for",
        "of",
        "to",
        "on",
        "in",
        "at",
        "by",
        "what",
        "when",
        "where",
        "how",
        "why",
        "who",
        "which",
        "me",
        "you",
        "your",
        "yourself",
        "my",
        "myself",
        "i",
        "we",
        "us",
        "our",
        "them",
        "they",
        "their",
        "it",
        "making",
        "trying",
        "sort",
        "kind",
        "exactly",
        "real",
        "really",
        "right",
        "now",
        "deep",
        "deeper",
        "ahead",
        "going",
        "getting",
        "goes",
        "went",
        "feel",
        "does",
        "did",
        "seems",
        "sounds",
        "looks",
    )
)

# Enough overlap with the user's previous words to count as a lifted clause.
_PARROT_OVERLAP_RATIO = 0.6
_PARROT_MIN_OVERLAP = 2
_PARROT_MIN_USER_WORDS = 3
# A mirror counts as a parrot when the non-echo remainder adds fewer than this
# many genuinely new concepts -- a therapist tail ("what's making that feel
# real right now?") contributes zero.
_PARROT_INFO_THRESHOLD = 2
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _content_tokens(text: str) -> set[str]:
    """Content words: all word tokens minus stopwords."""
    words = _TOKEN_RE.findall((text or "").casefold())
    return {w for w in words if w not in _STOPWORDS}


def _sentences(lower: str) -> list[str]:
    return [s for s in _SENTENCE_RE.split(lower) if s.strip()]


def _echo_shared(sentence_content: set[str], user_words: set[str]) -> int:
    return len(user_words & sentence_content)


def _parrot_eligible(reply: str, prev_user: str, lower: str) -> bool:
    """At least one sentence of the reply lifts a long clause from the
    previous user message. No question and no previous turn means there is
    nothing to parrot against, so the rule stays silent -- a mirror without a
    question is a compliant observation."""
    user_words = _content_tokens(prev_user)
    if len(user_words) < _PARROT_MIN_USER_WORDS:
        return False
    if "?" not in lower:
        return False
    return any(
        _echo_shared(_content_tokens(sentence), user_words) >= _PARROT_MIN_OVERLAP
        and _echo_shared(_content_tokens(sentence), user_words) / len(user_words)
        >= _PARROT_OVERLAP_RATIO
        for sentence in _sentences(lower)
    )


def _parrot_payload_adds_information(reply: str, prev_user: str, lower: str) -> bool:
    """Does the non-echo part of the reply add real concepts, or is it only
    a therapist-style filler after the lifted clause ("what's making that feel
    real right now?")?"""
    user_words = _content_tokens(prev_user)
    info: set[str] = set()
    for sentence in _sentences(lower):
        sentence_content = _content_tokens(sentence)
        if _echo_shared(sentence_content, user_words) >= _PARROT_MIN_OVERLAP:
            continue  # the lifted clause itself is not information
        for token in _TOKEN_RE.findall(sentence):
            if token in user_words or token in _STOPWORDS or token in _PROCESS_WORDS:
                continue
            info.add(token)
    return len(info) >= _PARROT_INFO_THRESHOLD


# Number tokens (dollars, percents, amounts, years) and list ordinals.
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*%?")
_ORDINAL_PREFIX_RE = re.compile(r"(?m)^[ \t]*\d+[.)][ \t]+")


def _normalize_number(match: str) -> str:
    """Digits + decimal, commas/currency/percent stripped. Decimal trailing
    zeros are trimmed so 1,500.00 == 1500; integer trailing zeros are
    significant, so "1500" is never mistaken for "15".

    A literal percent sign means a ratio, so it normalizes to its decimal form:
    "60%" and "0.6" are the same figure (a presenter must be able to say
    "sixty percent" next to a plan confidence of 0.6 without being flagged as
    inventing a number)."""
    percent = match.rstrip().endswith("%")
    clean = re.sub(r"[^0-9.]", "", match)
    if "." not in clean:
        normalized = clean or "0"
    else:
        whole, _, fraction = clean.partition(".")
        fraction = fraction.rstrip("0")
        normalized = f"{whole}.{fraction}" if fraction else whole or "0"
    if not percent:
        return normalized
    try:
        return str(round(float(normalized) / 100, 6))
    except ValueError:
        return normalized


def _grounded_set(text: str) -> set[str]:
    return {_normalize_number(t) for t in _NUMBER_TOKEN_RE.findall(text or "")}


def _ungrounded_numbers(reply: str, grounded: str) -> list[str]:
    """Numeric tokens in ``reply`` that have no match in the normalized
    ``grounded`` context. List ordinals ("1." line prefixes) are structural,
    not figures, so they are never flagged. A reply containing numbers against
    a grounding context with no numbers is entirely ungrounded."""
    if not grounded or not reply:
        return []
    ground = _grounded_set(grounded)
    cleaned = _ORDINAL_PREFIX_RE.sub("", reply)
    bad: list[str] = []
    for token in _NUMBER_TOKEN_RE.findall(cleaned):
        if _normalize_number(token) not in ground:
            bad.append(token)
    return bad


@dataclass
class EvalMeta:
    """What to evaluate a reply against, beyond the reply itself."""

    intent: str = "interview"
    has_taps: bool = False
    present: bool = False
    grounded: str = ""
    prev_user: str = ""


def evaluate_reply(reply: str, meta: EvalMeta | None = None) -> list[str]:
    """Return the spec rules a reply violates (sorted); [] means it conforms."""
    meta = meta or EvalMeta()
    text = (reply or "").strip()
    lower = text.casefold()
    words = len(text.split())
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]

    violations: list[str] = []
    limit = MAX_WORDS_PRESENT if meta.present else MAX_WORDS_CONVERSATIONAL
    if words > limit or len(paragraphs) > MAX_PARAGRAPHS:
        violations.append("R1")
    if text.count("?") > MAX_QUESTION_MARKS:
        violations.append("R2")
    if any(p in lower for p in _GENERIC_PRAISE):
        violations.append("R3")
    if any(p in lower for p in _GENERIC_ADVICE):
        violations.append("R4")
    if any(a in lower for a in _IDENTITY_ATTACKS):
        violations.append("R5")
    if any(b in lower for b in _CORPORATE_BOILERPLATE):
        violations.append("R6")
    if meta.has_taps and (len(text) > TAP_REPLY_MAX_CHARS or not text.endswith("?")):
        violations.append("R7")
    if any(s in lower for s in _MONEY_SCRIPT_LABELS):
        violations.append("R8")
    if not meta.present and _BULLET_RE.search(text):
        violations.append("R9")
    if meta.grounded and _ungrounded_numbers(text, meta.grounded):
        violations.append("R10")
    if prev_user := (meta.prev_user or "").strip():
        parrot = _parrot_eligible(text, prev_user, lower)
        if parrot and not _parrot_payload_adds_information(text, prev_user, lower):
            violations.append("R11")
    if any(p in lower for p in _THERAPIST_PHRASES):
        violations.append("R12")
    return sorted(violations)
