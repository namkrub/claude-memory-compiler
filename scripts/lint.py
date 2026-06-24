"""
Lint the knowledge base for structural and semantic health.

Runs 8 checks: broken links, orphan pages, orphan sources, stale articles,
date-staleness (personal memory), contradictions (LLM), missing backlinks,
and sparse articles.

Usage:
    uv run python lint.py                    # all checks
    uv run python lint.py --structural-only  # skip LLM checks (faster, cheaper)
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import date, timedelta
from pathlib import Path

from config import (
    KNOWLEDGE_DIR,
    PERSONAL_MEMORY_DIR,
    REPORTS_DIR,
    WORKSPACE_ROOT,
    now_iso,
    today_iso,
)
from utils import (
    count_inbound_links,
    extract_wikilinks,
    file_hash,
    get_article_word_count,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_all_wiki_content,
    save_state,
    wiki_article_exists,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


def check_broken_links() -> list[dict]:
    """Check for [[wikilinks]] that point to non-existent articles."""
    issues = []
    for article in list_wiki_articles():
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        for link in extract_wikilinks(content):
            if link.startswith("daily/"):
                continue  # daily log references are valid
            if not wiki_article_exists(link):
                issues.append({
                    "severity": "error",
                    "check": "broken_link",
                    "file": str(rel),
                    "detail": f"Broken link: [[{link}]] - target does not exist",
                })
    return issues


def check_orphan_pages() -> list[dict]:
    """Check for articles with zero inbound links."""
    issues = []
    for article in list_wiki_articles():
        rel = article.relative_to(KNOWLEDGE_DIR)
        link_target = str(rel).replace(".md", "").replace("\\", "/")
        inbound = count_inbound_links(link_target)
        if inbound == 0:
            issues.append({
                "severity": "warning",
                "check": "orphan_page",
                "file": str(rel),
                "detail": f"Orphan page: no other articles link to [[{link_target}]]",
            })
    return issues


def check_orphan_sources() -> list[dict]:
    """Check for daily logs that haven't been compiled yet."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files():
        if log_path.name not in ingested:
            issues.append({
                "severity": "warning",
                "check": "orphan_source",
                "file": f"daily/{log_path.name}",
                "detail": f"Uncompiled daily log: {log_path.name} has not been ingested",
            })
    return issues


def check_stale_articles() -> list[dict]:
    """Check if source daily logs have changed since compilation."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files():
        rel = log_path.name
        if rel in ingested:
            stored_hash = ingested[rel].get("hash", "")
            current_hash = file_hash(log_path)
            if stored_hash != current_hash:
                issues.append({
                    "severity": "warning",
                    "check": "stale_article",
                    "file": f"daily/{rel}",
                    "detail": f"Stale: {rel} has changed since last compilation",
                })
    return issues


def check_missing_backlinks() -> list[dict]:
    """Check for asymmetric links: A links to B but B doesn't link to A."""
    issues = []
    for article in list_wiki_articles():
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        source_link = str(rel).replace(".md", "").replace("\\", "/")

        for link in extract_wikilinks(content):
            if link.startswith("daily/"):
                continue
            target_path = KNOWLEDGE_DIR / f"{link}.md"
            if target_path.exists():
                target_content = target_path.read_text(encoding="utf-8")
                if f"[[{source_link}]]" not in target_content:
                    issues.append({
                        "severity": "suggestion",
                        "check": "missing_backlink",
                        "file": str(rel),
                        "detail": f"[[{source_link}]] links to [[{link}]] but not vice versa",
                        "auto_fixable": True,
                    })
    return issues


# ── Date-staleness (personal recall memory) ──────────────────────────────
# Catches the failure mode where a memory entry or index hook says something
# like "Monday 06-22 = agree scope" and that date is now in the past, so the
# entry reads as a pending commitment that already happened. Deterministic
# regex, no LLM. A past date alone is fine (historical record); it is only
# flagged when paired with a future-tense marker on the same line.

# Whole-word future-tense markers (so "agree" does not match "agreement").
_MARKER_RE = re.compile(
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"
    r"|\b(?:goal|agree|re-?check|next step|deadline|due|upcoming|to-?do|will)\b",
    re.IGNORECASE,
)

# A date preceded by one of these is a record stamp, not a pending commitment.
_STAMP_RE = re.compile(
    r"(?:confirmed|status|as of|updated|dated|posted|created|logged|recorded)\W*$",
    re.IGNORECASE,
)

# A line carrying a resolution stamp has already been addressed (per the
# outcome-on-write rule). Skip it so a preserved prediction does not flag
# forever. The index hook never carries a stamp, so hooks stay strict.
_RESOLUTION_STAMP_RE = re.compile(
    r"\[(?:resolved|done|falsified|rescheduled|archived)\b", re.IGNORECASE
)

_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")        # 2026-06-22
_DOT_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2}\b")  # 06.22.26 (workspace std)
_DASH3_RE = re.compile(r"\b\d{1,2}-\d{1,2}-\d{2}\b")  # 06-22-26
_MD_RE = re.compile(r"\b\d{1,2}-\d{1,2}\b")           # 06-22 (year inferred)


def _token_to_date(token: str, today: date) -> date | None:
    """Parse a date token to a date. Bare MM-DD infers the year: current year,
    rolled back one if that would land more than ~6 months in the future."""
    try:
        if _ISO_RE.fullmatch(token):
            y, m, d = (int(x) for x in token.split("-"))
            return date(y, m, d)
        if _DOT_RE.fullmatch(token):
            m, d, yy = (int(x) for x in token.split("."))
            return date(2000 + yy, m, d)
        if _DASH3_RE.fullmatch(token):
            m, d, yy = (int(x) for x in token.split("-"))
            return date(2000 + yy, m, d)
        if _MD_RE.fullmatch(token):
            m, d = (int(x) for x in token.split("-"))
            cand = date(today.year, m, d)
            if cand - today > timedelta(days=180):
                cand = date(today.year - 1, m, d)
            return cand
    except ValueError:
        return None
    return None


def find_stale_dated_commitments(content: str, today: date) -> list[dict]:
    """Find lines with a past date next to a future-tense marker."""
    findings = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if _RESOLUTION_STAMP_RE.search(line):
            continue  # already addressed; preserved history is not stale
        if not _MARKER_RE.search(line):
            continue
        covered: list[tuple[int, int]] = []
        # Longer patterns first so a bare MM-DD inside an ISO date is not
        # double-counted.
        for rgx in (_ISO_RE, _DOT_RE, _DASH3_RE, _MD_RE):
            for m in rgx.finditer(line):
                s, e = m.span()
                if any(s >= cs and e <= ce for cs, ce in covered):
                    continue
                # Skip dates embedded in a path or filename (slug-MM.DD.YY.md,
                # dir/2026-01-31-x). Those are records, not pending commitments.
                before = line[s - 1] if s > 0 else ""
                after = line[e] if e < len(line) else ""
                if before in "/-_." or after in "/_-":
                    continue
                # Skip record stamps: "(confirmed 06.14.26)", "(status 2026-..)".
                if _STAMP_RE.search(line[max(0, s - 20):s]):
                    continue
                dt = _token_to_date(m.group(), today)
                if dt is None:
                    continue
                covered.append((s, e))
                if dt < today:
                    findings.append({
                        "line": lineno,
                        "detail": (
                            f"Line {lineno}: past date {m.group()} with a "
                            f"future-tense marker (stale commitment?): "
                            f"{line.strip()[:120]}"
                        ),
                    })
    return findings


def check_personal_memory_staleness(
    memory_dir: Path | None = None, today: date | None = None
) -> list[dict]:
    """Scan the personal recall memory dir for stale dated commitments."""
    base = Path(memory_dir) if memory_dir is not None else PERSONAL_MEMORY_DIR
    if today is None:
        today = date.fromisoformat(today_iso())
    if not base.exists():
        return []
    issues = []
    for md in sorted(base.rglob("*.md")):
        content = md.read_text(encoding="utf-8")
        for hit in find_stale_dated_commitments(content, today):
            issues.append({
                "severity": "warning",
                "check": "date_staleness",
                "file": str(md),
                "detail": hit["detail"],
            })
    return issues


def check_sparse_articles() -> list[dict]:
    """Check for articles with fewer than 200 words."""
    issues = []
    for article in list_wiki_articles():
        word_count = get_article_word_count(article)
        if word_count < 200:
            rel = article.relative_to(KNOWLEDGE_DIR)
            issues.append({
                "severity": "suggestion",
                "check": "sparse_article",
                "file": str(rel),
                "detail": f"Sparse article: {word_count} words (minimum recommended: 200)",
            })
    return issues


async def check_contradictions() -> list[dict]:
    """Use LLM to detect contradictions across articles."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    wiki_content = read_all_wiki_content()

    prompt = f"""Review this knowledge base for contradictions, inconsistencies, or
conflicting claims across articles.

## Knowledge Base

{wiki_content}

## Instructions

Look for:
- Direct contradictions (article A says X, article B says not-X)
- Inconsistent recommendations (different articles recommend conflicting approaches)
- Outdated information that conflicts with newer entries

For each issue found, output EXACTLY one line in this format:
CONTRADICTION: [file1] vs [file2] - description of the conflict
INCONSISTENCY: [file] - description of the inconsistency

If no issues found, output exactly: NO_ISSUES

Do NOT output anything else - no preamble, no explanation, just the formatted lines."""

    response = ""
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                allowed_tools=[],
                max_turns=2,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
    except Exception as e:
        return [{"severity": "error", "check": "contradiction", "file": "(system)", "detail": f"LLM check failed: {e}"}]

    issues = []
    if "NO_ISSUES" not in response:
        for line in response.strip().split("\n"):
            line = line.strip()
            if line.startswith("CONTRADICTION:") or line.startswith("INCONSISTENCY:"):
                issues.append({
                    "severity": "warning",
                    "check": "contradiction",
                    "file": "(cross-article)",
                    "detail": line,
                })

    return issues


def generate_report(all_issues: list[dict]) -> str:
    """Generate a markdown lint report."""
    errors = [i for i in all_issues if i["severity"] == "error"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]
    suggestions = [i for i in all_issues if i["severity"] == "suggestion"]

    lines = [
        f"# Lint Report - {today_iso()}",
        "",
        f"**Total issues:** {len(all_issues)}",
        f"- Errors: {len(errors)}",
        f"- Warnings: {len(warnings)}",
        f"- Suggestions: {len(suggestions)}",
        "",
    ]

    for severity, issues, marker in [
        ("Errors", errors, "x"),
        ("Warnings", warnings, "!"),
        ("Suggestions", suggestions, "?"),
    ]:
        if issues:
            lines.append(f"## {severity}")
            lines.append("")
            for issue in issues:
                fixable = " (auto-fixable)" if issue.get("auto_fixable") else ""
                lines.append(f"- **[{marker}]** `{issue['file']}` - {issue['detail']}{fixable}")
            lines.append("")

    if not all_issues:
        lines.append("All checks passed. Knowledge base is healthy.")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Lint the knowledge base")
    parser.add_argument(
        "--structural-only",
        action="store_true",
        help="Skip LLM-based checks (contradictions) - faster and free",
    )
    args = parser.parse_args()

    print("Running knowledge base lint checks...")
    all_issues: list[dict] = []

    # Structural checks (free, instant)
    checks = [
        ("Broken links", check_broken_links),
        ("Orphan pages", check_orphan_pages),
        ("Orphan sources", check_orphan_sources),
        ("Stale articles", check_stale_articles),
        ("Date staleness (personal memory)", check_personal_memory_staleness),
        ("Missing backlinks", check_missing_backlinks),
        ("Sparse articles", check_sparse_articles),
    ]

    for name, check_fn in checks:
        print(f"  Checking: {name}...")
        issues = check_fn()
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")

    # LLM check (costs money)
    if not args.structural_only:
        print("  Checking: Contradictions (LLM)...")
        issues = asyncio.run(check_contradictions())
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")
    else:
        print("  Skipping: Contradictions (--structural-only)")

    # Generate and save report
    report = generate_report(all_issues)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"lint-{today_iso()}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {report_path}")

    # Update state
    state = load_state()
    state["last_lint"] = now_iso()
    save_state(state)

    # Summary
    errors = sum(1 for i in all_issues if i["severity"] == "error")
    warnings = sum(1 for i in all_issues if i["severity"] == "warning")
    suggestions = sum(1 for i in all_issues if i["severity"] == "suggestion")
    print(f"\nResults: {errors} errors, {warnings} warnings, {suggestions} suggestions")

    # Append a one-line audit entry to workspace-level log.md so the run is
    # discoverable outside the lint-reports directory. Best-effort; never fails
    # the run if the append itself errors (read-only fs, permissions, etc.).
    try:
        log_path = WORKSPACE_ROOT / "log.md"
        rel_report = report_path.relative_to(WORKSPACE_ROOT)
        summary = f"{errors} errors, {warnings} warnings, {suggestions} suggestions"
        line = f"\n## [{today_iso()}] lint | {summary}; see {rel_report}\n"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        print(f"Warning: could not append to log.md: {exc}")

    if errors > 0:
        print("\nErrors found - knowledge base needs attention!")
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
