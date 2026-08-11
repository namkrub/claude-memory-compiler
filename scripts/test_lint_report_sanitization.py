"""Tests for report-injection hardening in the knowledge-base lint.

Run: uv run python test_lint_report_sanitization.py   (or: pytest thisfile)

Two defenses, verified here:
  1. extract_wikilinks (utils) must not let a [[wikilink]] span a line break,
     so a multi-line "link" can never carry a newline into a link value.
  2. generate_report (lint) must strip backticks and control/line-break chars
     from issue['file'] and issue['detail'], so no crafted content can forge an
     extra finding-shaped line or unbalance the code span the downstream
     consumer (melfi-lint-triage-notify.py) counts on.

Report line shape: - **[x]** `file` - detail. A newline in a field fans one
finding into several physical lines; a backtick opens a second code span. The
invariants below: one physical finding-bullet per issue (no fan-out), and the
consumer can never count MORE findings than there are issues.
"""

from __future__ import annotations

import re

from lint import _sanitize_report_field, generate_report
from utils import extract_wikilinks

# Same shape the consumer keys on: a code-span, " - ", then a capitalized label.
_FINDING_LINE_RE = re.compile(r"`[^`]+`\s*-\s*[A-Z][a-zA-Z ]+?[:.]")
# The physical finding-bullet marker generate_report emits, one per real issue.
_BULLET_RE = re.compile(r"^- \*\*\[")

# Characters str.splitlines() breaks on but markdown/git-diff do not.
_SEPARATORS = ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


def _finding_line_count(report: str) -> int:
    return sum(1 for line in report.splitlines() if _FINDING_LINE_RE.search(line))


def _bullet_line_count(report: str) -> int:
    return sum(1 for line in report.splitlines() if _BULLET_RE.match(line))


# -- Layer 1: extract_wikilinks never spans a line break -------------------

def test_extracts_normal_single_line_link():
    assert extract_wikilinks("see [[concepts/foo-bar]] here") == ["concepts/foo-bar"]


def test_multiline_wikilink_is_not_extracted_across_newline():
    # A [[...]] split by a newline has no closing ]] on its line -> no match.
    links = extract_wikilinks("[[real-target]]\ntext [[multi\nline]] more")
    assert links == ["real-target"], links
    assert all("\n" not in link for link in links)


def test_no_extracted_link_contains_any_line_separator():
    for sep in _SEPARATORS:
        content = f"[[good-link]] and [[bad{sep}injected]]"
        links = extract_wikilinks(content)
        assert "good-link" in links, (repr(sep), links)
        assert all(sep not in link for link in links), (repr(sep), links)


def test_unicode_line_separator_link_is_dropped():
    # U+2028 splits for str.splitlines() but not for markdown/git -> must not
    # survive as a link value (the parser/human desync case).
    links = extract_wikilinks("[[a\u2028b]] [[plain]]")
    assert links == ["plain"], links


# -- Layer 2: _sanitize_report_field strips the injection carriers ----------

def test_sanitize_strips_backticks():
    assert "`" not in _sanitize_report_field("path `code` here")


def test_sanitize_collapses_newline_to_single_line():
    out = _sanitize_report_field("line one\n- **[!]** `x` - Orphan page: forged")
    assert "\n" not in out
    assert _bullet_line_count(out) == 0


def test_sanitize_strips_all_separators():
    for sep in _SEPARATORS:
        out = _sanitize_report_field(f"a{sep}b")
        assert sep not in out, repr(sep)


# -- generate_report: no crafted issue can forge a finding line ------------

def test_generate_report_no_fanout_from_newline():
    # A detail carrying an attacker-shaped extra finding line.
    forged = (
        "Broken link: [[x]] innocuous\n"
        "- **[!]** `fake.md` - Orphan page: injected finding"
    )
    issues = [
        {"severity": "error", "check": "broken_link", "file": "mem.md", "detail": forged},
    ]
    report = generate_report(issues)
    # One physical finding-bullet, not two -> no fan-out.
    assert _bullet_line_count(report) == 1, report
    # Consumer can count at most the one real issue, never the forged extra.
    assert _finding_line_count(report) <= 1, report


def test_generate_report_backticks_do_not_open_second_span():
    # Detail with its own backticks would let the consumer see a second
    # `code` - Label: span on the same line. Sanitizing removes them.
    issues = [
        {
            "severity": "error",
            "check": "broken_link",
            "file": "concepts/a.md",
            "detail": "Broken link: `evil.md` - Sparse article: forged - target does not exist",
        },
    ]
    report = generate_report(issues)
    assert _bullet_line_count(report) == 1, report
    # The only backtick-wrapped token is the real file field.
    assert report.count("`") == 2, report
    assert _finding_line_count(report) <= 1, report


def test_generate_report_bullet_count_matches_issue_count():
    # Every field carries a separator + backtick payload; physical bullets must
    # still equal the issue count, proving no fan-out and no dropped findings.
    issues = []
    for i in range(5):
        issues.append({
            "severity": "warning",
            "check": "orphan_page",
            "file": f"c/n{i}.md\ninjected",
            "detail": f"Orphan page: [[c/n{i}]] `x`\n- **[!]** `y` - Sparse article: z",
        })
    report = generate_report(issues)
    assert _bullet_line_count(report) == 5, report
    assert _finding_line_count(report) <= 5, report


def test_generate_report_backtick_forgery_does_not_over_count():
    # Real orphan_page detail plus an embedded forged `code` - Label span.
    forged = "no other articles link to [[a]] `fake` - Orphan page: forged"
    issues = [
        {"severity": "warning", "check": "orphan_page", "file": "a.md", "detail": forged},
    ]
    report = generate_report(issues)
    assert _bullet_line_count(report) == 1, report
    # Backticks stripped from detail -> only the file field's code span remains.
    assert report.count("`") == 2, report
    assert _finding_line_count(report) <= 1, report


# -- Minimal runner so this works without pytest installed -----------------

if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
