"""Pure helpers for the ``search_natural`` tool.

Split out of ``server.py`` so each transformation can be unit-tested without
spinning up the MCP context. Anything that needs the live HTTP client stays
in ``server.py``.
"""

from __future__ import annotations

import re

from .models import CandidateQuery, TemplateHint

# Static reference returned to the host LLM in Mode A. Examples follow the
# syntax actually accepted by self-hosted LFRepositoryAPI SimpleSearches —
# do not invent operators beyond what is listed here.
LF_GRAMMAR_REFERENCE = """\
Laserfiche search syntax (self-hosted Repository API)

Each clause is wrapped in braces. Combine clauses with `&` (AND) or `|` (OR).

Name search (case-insensitive, wildcards * and ?):
  {LF:Name="Onboarding*"}
  {LF:Name="*.pdf"}
  {LF:Name="Smith?Jane"}

Path scoping (limits the search to a folder subtree):
  {LF:LookIn="\\\\Imports\\\\2024"}

Template field search (template name and field name in their own brackets):
  {[Missionary Application]:[Last Name]="Smith"}
  {[Missionary Application]:[Submitted Date]>=02/01/2024}

Combined:
  {LF:Name="*.pdf"} & {[Application]:[Status]="Approved"}
  {LF:LookIn="\\\\HR"} & {[Personnel]:[Hire Year]=2025}

Quoting:
  Double quotes are required around string values.
  Embedded quotes inside a value must be escaped as \\"

Wildcards:
  * matches any sequence (including empty).
  ? matches one character.
  No wildcards in a Name= clause = exact match.
"""


# Captures the value inside ``{LF:Name="..."}``. The inner group accepts
# either an escaped quote ``\"`` or any non-``"`` character — that way a
# value already touched up by ``repair_escape_quotes`` (e.g. ``o\"hare``)
# still matches and can pick up wildcard wrapping on the next pass.
_NAME_VALUE_RE = re.compile(r'\{LF:Name="((?:\\"|[^"])*)"\}')


def repair_escape_quotes(query: str) -> str | None:
    """Escape unescaped ``"`` inside ``="..."`` value spans.

    Returns the rewritten query, or ``None`` if no change was needed. A
    closing quote is recognized when followed (after optional whitespace) by
    ``}``, ``&``, ``|`` or end-of-string — anything else inside the value
    span is treated as content and escaped.

    The walk is intentionally a hand-rolled state machine, not a regex,
    because the closing-quote heuristic depends on lookahead context.
    """
    out: list[str] = []
    i = 0
    n = len(query)
    in_value = False
    changed = False

    while i < n:
        c = query[i]
        if not in_value:
            out.append(c)
            # Entering a value span: ="
            if c == "=" and i + 1 < n and query[i + 1] == '"':
                out.append('"')
                i += 2
                in_value = True
                continue
            i += 1
            continue

        # in_value
        if c == "\\" and i + 1 < n:
            # Already-escaped char — pass through untouched.
            out.append(c)
            out.append(query[i + 1])
            i += 2
            continue

        if c == '"':
            # Could be the closing quote or an internal quote.
            j = i + 1
            while j < n and query[j] == " ":
                j += 1
            if j == n or query[j] in "}&|":
                out.append('"')
                in_value = False
                i += 1
                continue
            # Internal quote — escape it.
            out.append('\\"')
            changed = True
            i += 1
            continue

        out.append(c)
        i += 1

    return "".join(out) if changed else None


def repair_wildcard_name(query: str) -> str | None:
    """Wrap Name= values in ``*`` wildcards when none are present.

    Returns the rewritten query, or ``None`` if every ``{LF:Name="..."}``
    clause already contains a ``*`` or ``?``. Only Name clauses are touched;
    template field clauses are left alone.
    """
    changed = False

    def _wrap(match: re.Match[str]) -> str:
        nonlocal changed
        value = match.group(1)
        if "*" in value or "?" in value or not value:
            return match.group(0)
        changed = True
        return f'{{LF:Name="*{value}*"}}'

    rewritten = _NAME_VALUE_RE.sub(_wrap, query)
    return rewritten if changed else None


_KEYWORD_RE = re.compile(r"\b([A-Z][\w'-]{1,}|[A-Z]{2,})\b")
# Common English connector words that look like proper nouns but aren't useful
# as Name= keywords.
_KEYWORD_STOP = {
    "The",
    "A",
    "An",
    "And",
    "Or",
    "Of",
    "For",
    "To",
    "From",
    "By",
    "With",
    "Find",
    "Show",
    "Get",
    "Where",
    "What",
    "When",
    "Which",
    "Who",
    "Whose",
}


def extract_keywords(question: str) -> list[str]:
    """Pull proper-noun-ish keywords out of the user's question.

    Order-preserving and deduplicated. Trailing English possessives (``'s``)
    are stripped so ``"Smith's"`` becomes ``"Smith"`` while ``"O'Brien"``
    survives intact. Used only as input to the heuristic candidate-query
    generator below — the host LLM is expected to refine these before
    calling Mode B.
    """
    seen: set[str] = set()
    keywords: list[str] = []
    for match in _KEYWORD_RE.finditer(question):
        word = match.group(1)
        if word.endswith("'s"):
            word = word[:-2]
        if not word or word in _KEYWORD_STOP or word in seen:
            continue
        seen.add(word)
        keywords.append(word)
    return keywords


def build_candidate_queries(
    question: str,
    folder_path: str | None,
    templates: list[TemplateHint],
) -> list[CandidateQuery]:
    """Generate a small set of starter Laserfiche queries.

    These are heuristic — the host LLM is expected to pick one and refine.
    The function deliberately returns at most three candidates so the
    response stays small in the LLM's context.
    """
    candidates: list[CandidateQuery] = []
    keywords = extract_keywords(question)
    path_clause = f' & {{LF:LookIn="{folder_path}"}}' if folder_path else ""

    # Candidate 1: fuzzy name match on the first keyword.
    if keywords:
        first = keywords[0]
        candidates.append(
            CandidateQuery(
                query=f'{{LF:Name="*{first}*"}}{path_clause}',
                rationale=(
                    f'Wildcard name match on "{first}" '
                    + ("scoped to folder." if folder_path else "across the repository.")
                ),
            )
        )

    # Candidate 2: AND across the first two keywords if available.
    if len(keywords) >= 2:
        first, second = keywords[0], keywords[1]
        candidates.append(
            CandidateQuery(
                query=(f'{{LF:Name="*{first}*"}} & {{LF:Name="*{second}*"}}{path_clause}'),
                rationale=(
                    f'Both "{first}" and "{second}" must appear in the name. '
                    "Tighter than a single-keyword match."
                ),
            )
        )

    # Candidate 3: templated field query if we sampled at least one template.
    if templates and keywords:
        template = templates[0]
        # Prefer a field that sounds name-ish; otherwise first field.
        name_like_fields = [
            f
            for f in template.field_names
            if any(tok in f.lower() for tok in ("name", "person", "last", "first"))
        ]
        field = (
            name_like_fields[0]
            if name_like_fields
            else (template.field_names[0] if template.field_names else None)
        )
        if field:
            first = keywords[0]
            candidates.append(
                CandidateQuery(
                    query=(f'{{[{template.template_name}]:[{field}]="*{first}*"}}{path_clause}'),
                    rationale=(
                        f'Match against the "{field}" field on the '
                        f'"{template.template_name}" template — sampled '
                        f"from {folder_path or 'the repository root'}."
                    ),
                )
            )

    # Fallback so we never return an empty list.
    if not candidates:
        candidates.append(
            CandidateQuery(
                query=f'{{LF:Name="*"}}{path_clause}',
                rationale=(
                    "No keywords were extractable from the question; this "
                    "matches everything "
                    + (f"under {folder_path}." if folder_path else "in the repository.")
                ),
            )
        )

    return candidates[:3]
