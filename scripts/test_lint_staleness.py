"""Tests for the date-staleness lint check (personal recall memory).

Run: uv run python test_lint_staleness.py   (or: pytest test_lint_staleness.py)

Covers the pure detector find_stale_dated_commitments() and the
check_personal_memory_staleness() file-scanning wrapper.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from lint import check_personal_memory_staleness, find_stale_dated_commitments

TODAY = date(2026, 6, 23)


# ── Pure detector ────────────────────────────────────────────────────────

def test_flags_past_iso_date_with_future_marker():
    content = "Monday (2026-06-22) goal with Andy: agree scope."
    hits = find_stale_dated_commitments(content, TODAY)
    assert len(hits) == 1, hits
    assert "2026-06-22" in hits[0]["detail"]


def test_ignores_future_iso_date_with_marker():
    content = "Monday (2026-07-01) goal: ship the thing."
    assert find_stale_dated_commitments(content, TODAY) == []


def test_ignores_past_date_without_future_marker():
    # A historical record is fine; only pending-tense + past date is stale.
    content = "On 2026-06-19 El Jefe changed direction on the engagement."
    assert find_stale_dated_commitments(content, TODAY) == []


def test_flags_bare_month_day_with_marker():
    # The index-hook form that actually misled a session.
    content = "Monday 06-22 = agree scope/criteria with Andy"
    hits = find_stale_dated_commitments(content, TODAY)
    assert len(hits) == 1, hits


def test_does_not_double_count_month_day_inside_iso():
    content = "re-check after 2026-06-22 once Andy responds"
    hits = find_stale_dated_commitments(content, TODAY)
    # One finding for the one stale date, not two (06-22 must not also match).
    assert len(hits) == 1, hits


def test_clean_content_has_no_flags():
    content = "Success = incremental cross-sell, not aggregate revenue."
    assert find_stale_dated_commitments(content, TODAY) == []


def test_ignores_dates_inside_filenames():
    # Workspace files are named slug-MM.DD.YY.md, so every file reference
    # carries a past date. Those are records, not pending commitments.
    content = "Deliverables: `foo/bar-monday-prep-06.19.26.md`, `baz-05.31.26.md`"
    assert find_stale_dated_commitments(content, TODAY) == []


def test_ignores_iso_date_inside_path():
    content = "**The lead:** `Bureau/memory/imports/2026-01-31-part-2.md` will matter"
    assert find_stale_dated_commitments(content, TODAY) == []


def test_marker_must_be_whole_word():
    # "agreement"/"agreed" contain "agree" but are not future-tense markers.
    content = "No data agreement was reached on 2026-06-20."
    assert find_stale_dated_commitments(content, TODAY) == []


def test_ignores_confirmed_or_status_date_stamps():
    # "(confirmed DATE)" / "(status DATE)" are records, not pending commitments,
    # even when a real marker sits elsewhere on the line.
    confirmed = "Contracts due, not on hand (confirmed 06.14.26)."
    status = "**Falsifier (status 2026-06-22):** re-check once Andy responds."
    assert find_stale_dated_commitments(confirmed, TODAY) == []
    assert find_stale_dated_commitments(status, TODAY) == []


# ── File-scanning wrapper ────────────────────────────────────────────────

def test_check_scans_dir_and_returns_issue_shape(tmp_path: Path):
    (tmp_path / "stale.md").write_text(
        "Monday (2026-06-22) goal: agree scope.", encoding="utf-8"
    )
    (tmp_path / "clean.md").write_text(
        "Durable strategy note, no dates.", encoding="utf-8"
    )
    issues = check_personal_memory_staleness(memory_dir=tmp_path, today=TODAY)
    assert len(issues) == 1, issues
    issue = issues[0]
    assert issue["check"] == "date_staleness"
    assert issue["severity"] == "warning"
    assert issue["file"].endswith("stale.md")


def test_check_missing_dir_returns_empty(tmp_path: Path):
    missing = tmp_path / "nope"
    assert check_personal_memory_staleness(memory_dir=missing, today=TODAY) == []


# ── Minimal runner so this works without pytest installed ─────────────────

if __name__ == "__main__":
    import tempfile
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS {name}")
            passed += 1
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
