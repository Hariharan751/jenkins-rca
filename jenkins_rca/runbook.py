"""Smart loader for runbook markdown files.

A full runbook (e.g. JiraDetailsCompliance.md, 16KB) won't fit in the prompt
budget. Naive truncation to first N chars usually grabs intro/usage but misses
the failure remediation section we actually need for RCA.

Strategy:
- Split doc into sections by markdown headers (#, ##, ###).
- Score each section by relevance keywords (failure, error, mismatch, fix,
  troubleshoot, etc.) and the error_class itself.
- Always include the doc's first section (title + intro for grounding).
- Include top-scored sections until char budget exhausted.
"""
import re


_SECTION_RE = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)

# Keywords that boost section relevance for RCA context
_RELEVANCE_KEYWORDS = (
    "fail", "failure", "error", "mismatch", "does not match",
    "fix", "remediation", "remedy", "troubleshoot", "troubleshooting",
    "resolve", "recovery", "rollback",
    "common issues", "known issues",
    "when this", "if this", "case of",
)


def _split_sections(doc: str) -> list[tuple[int, str, str]]:
    """Return list of (level, title, body) sections in document order."""
    matches = list(_SECTION_RE.finditer(doc))
    if not matches:
        return [(0, "doc", doc)]

    sections = []
    # Preamble before first header
    if matches[0].start() > 0:
        preamble = doc[:matches[0].start()].strip()
        if preamble:
            sections.append((0, "preamble", preamble))

    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(doc)
        body = doc[start:end].strip()
        sections.append((level, title, body))

    return sections


def _score_section(title: str, body: str, error_class: str) -> int:
    """Heuristic: higher = more useful for current RCA."""
    s = (title + "\n" + body).lower()
    score = 0
    for kw in _RELEVANCE_KEYWORDS:
        if kw in s:
            score += 3
    # Boost by error_class match
    if error_class and error_class.lower() in s:
        score += 5
    # Penalize purely structural sections (usage, parameters, etc.) unless
    # they also mention failure
    if any(t in title.lower() for t in ("usage", "parameter", "syntax")) and score == 0:
        score -= 1
    return score


def extract_relevant(doc: str, error_class: str, budget_chars: int = 6000) -> str:
    """Return concatenated sections fitting within budget. Always includes
    the first (title/intro) section to ground the LLM; remaining budget filled
    by highest-scoring sections."""
    if not doc:
        return ""
    if len(doc) <= budget_chars:
        return doc

    sections = _split_sections(doc)
    if not sections:
        return doc[:budget_chars]

    # Always include the first section (title + first paragraph)
    out_parts: list[str] = []
    head_level, head_title, head_body = sections[0]
    head_str = (f"{'#' * max(head_level, 1)} {head_title}\n{head_body}"
                if head_level > 0 else head_body)
    out_parts.append(head_str[:1500])
    used = len(out_parts[0])

    # Score remaining
    scored = []
    for i, (lvl, title, body) in enumerate(sections[1:], start=1):
        score = _score_section(title, body, error_class)
        scored.append((score, i, lvl, title, body))
    scored.sort(key=lambda x: (-x[0], x[1]))  # highest score, earliest first

    for score, _i, lvl, title, body in scored:
        if score <= 0:
            break
        chunk = f"\n\n{'#' * lvl} {title}\n{body}"
        if used + len(chunk) > budget_chars:
            # try a truncated version of this chunk
            remaining = budget_chars - used
            if remaining > 300:
                out_parts.append(chunk[:remaining])
            break
        out_parts.append(chunk)
        used += len(chunk)

    return "".join(out_parts)
