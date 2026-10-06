"""Input sanitizing helpers for CMN-C2-278.

Pure, stateless domain helpers (NOT framework gate methods), shared by the
outer backbone pre_process and the inner validate step so that BOTH caller
channels get the same treatment.

That sharing is the point. The framework's own input scan covers the
instruction text but not the structured caller channel, so a screen wired only
into the instruction path would leave caller-supplied data unscreened.
"""

from __future__ import annotations

import re

# HTML-ish markup and chat-template token frames. The angle-bracket strip
# covers pasted markup as well as `<|...|>` control-token frames, so it must
# never run BEFORE the override screen has seen the raw text: stripping first
# would turn a recognisable control token into ordinary-looking prose and
# forward the directive residue unflagged.
_MARKUP_RE = re.compile(r"<[^>]{1,500}>")

# Email addresses and bearer/JWT/API-token-like strings that might appear in a
# pasted request. These are flagged and redacted before anything is logged or
# forwarded.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

DEFAULT_MAX_LENGTH = 4000

# ── Template-owned instruction-override screen ────────────────────────────────
# Request text is this agent's PRODUCT: callers legitimately write "please
# ignore my previous request", "forget the earlier dates", "my manager
# overrode the rules and approved it" — ordinary leave-request prose is full of
# directive verbs. A substring screen would refuse real work, so every
# alternative here is anchored on a full directive PHRASE aimed at the MODEL
# (an override verb plus a prompt/instruction noun, or a model role), or on a
# chat-template control token, which has no legitimate reading in a leave
# request.
#
# This screen is the template's own guarantee, not the framework's: the
# platform input gate covers only user_input/validated_input (never the
# structured caller channel), rejects only HIGH-confidence findings, and is
# not guaranteed to be active on every host — wherever it is absent the
# template would otherwise fail OPEN.
_INSTRUCTION_OVERRIDE_RE = re.compile(
    # Chat-template control tokens: <|im_start|>, <|system|>, [INST], <<SYS>>.
    r"<\|[a-z_]{2,32}\|>"
    r"|\[/?INST\]"
    r"|<</?SYS>>"
    # "ignore/disregard/forget ... previous/... instructions/prompt/rules".
    # The noun class is instruction-nouns ONLY — never "request" or "leave",
    # so "please ignore my previous request" (real leave-request prose) passes.
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+|my\s+)*"
    r"(?:previous|prior|above|earlier|preceding|original|system)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)\b"
    # bare "ignore all rules/instructions" (no temporal qualifier)
    r"|\b(?:ignore|disregard)\s+all\s+(?:rules|instructions)\b"
    # exfiltration: "reveal/print your system prompt / the hidden instructions".
    # "show my remaining days" never matches — the object must be a
    # prompt/instruction noun with a system/initial/hidden qualifier.
    r"|\b(?:reveal|show|print|repeat|output|disclose|display|dump)\s+(?:me\s+)?"
    r"(?:your\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|prompts|instructions)"
    r"|the\s+(?:system|initial|hidden)\s+(?:prompt|prompts|instructions|message))\b"
    # role reassignment: requires a MODEL role, so "you are now the approver"
    # (a person's role) passes.
    r"|\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:different\s+|unrestricted\s+|new\s+|jailbroken\s+)?"
    r"(?:assistant|ai|chatbot|language\s+model|llm|dan)\b"
    # privileged-mode role-play: developer/admin/root + "mode". "acting as the
    # HR administrator" never matches — "acting" is not the whole word "act".
    r"|\bact\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:a\s+|an\s+)?"
    r"(?:developer|admin|administrator|root|jailbroken|unrestricted)\s+mode\b"
    # rule-override: "override your/the instructions/safety/guardrails" —
    # "the override was approved by my manager" has no instruction-noun object
    # and passes.
    r"|\boverride\s+(?:your|the)\s+"
    r"(?:instruction|instructions|rule|rules|safety|guardrail|guardrails|restriction|restrictions)\b"
    # "new system prompt:" header form
    r"|\b(?:new|updated)\s+system\s+(?:prompt|instructions)\s*[:=]",
    re.IGNORECASE,
)


def contains_instruction_override(text: str) -> bool:
    """True when the text carries an instruction-override directive.

    Screens the text BOTH as received and after the markup strip: the raw form
    catches chat-template control tokens before ``sanitize_query`` would remove
    them, and the stripped form catches a directive spliced with markup
    (``ig<b>nore``) that would otherwise re-assemble into the forwarded text.
    """
    if _INSTRUCTION_OVERRIDE_RE.search(text):
        return True
    stripped = _MARKUP_RE.sub("", text)
    return stripped != text and bool(_INSTRUCTION_OVERRIDE_RE.search(stripped))


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip markup/control sequences (injection guard) and cap length."""
    cleaned = _MARKUP_RE.sub("", query)
    return cleaned[:max_length]


def redact_sensitive(text: str) -> "tuple[str, list[str]]":
    """Flag-and-redact emails / token-like strings. Returns (redacted, flags).

    Flag-and-redact rather than reject: a leave request legitimately names
    people, so the hard rejections in this template are the empty /
    non-request guard and the instruction-override screen above. The flags name
    the CATEGORY found, never the value, so they are safe to log.
    """
    flags: list[str] = []
    redacted = text
    if _EMAIL_RE.search(redacted):
        flags.append("email")
        redacted = _EMAIL_RE.sub(_REDACTION, redacted)
    if _TOKEN_RE.search(redacted):
        flags.append("token")
        redacted = _TOKEN_RE.sub(_REDACTION, redacted)
    return redacted, flags
