"""Tests for the machine-readable JSON sidecar the lint emits (producer side).

Run: uv run python test_lint_sidecar.py   (or: pytest test_lint_sidecar.py)

The sidecar is the machine contract the downstream triage consumer counts on.
These tests lock the producer half:

  1. Counts are built from the code-controlled issue['check'] enum and match the
     raw issue list exactly (per-check, per-severity, and total).
  2. Crafted content in free-text fields (file/detail) cannot change any
     enum-keyed count -- counting never reads detail.
  3. A check value outside the known enum surfaces as its own key, so the
     consumer can detect producer/consumer vocabulary drift.

Zero-dependency runner at the bottom mirrors test_lint_staleness.py so it runs
via `python3 test_lint_sidecar.py` without pytest.
"""

from __future__ import annotations

from lint import CHECK_NAMES, SEVERITY_NAMES, SIDECAR_SCHEMA_VERSION, build_sidecar


def _issue(check: str, severity: str, file: str = "f.md", detail: str = "d") -> dict:
    return {"check": check, "severity": severity, "file": file, "detail": detail}


# -- Shape + known-vocab seeding -------------------------------------------

def test_sidecar_has_expected_top_level_shape():
    s = build_sidecar([])
    assert s["schema_version"] == SIDECAR_SCHEMA_VERSION
    assert isinstance(s["date"], str)
    assert isinstance(s["generated_at"], str)
    assert s["total"] == 0
    assert isinstance(s["check_counts"], dict)
    assert isinstance(s["severity_totals"], dict)


def test_all_known_checks_seeded_to_zero_when_empty():
    s = build_sidecar([])
    for name in CHECK_NAMES:
        assert s["check_counts"][name] == 0, name
    for name in SEVERITY_NAMES:
        assert s["severity_totals"][name] == 0, name


# -- Counts match the issue list exactly -----------------------------------

def test_check_counts_match_issue_list_exactly():
    issues = [
        _issue("orphan_page", "warning"),
        _issue("orphan_page", "warning"),
        _issue("sparse_article", "suggestion"),
        _issue("broken_link", "error"),
        _issue("date_staleness", "warning"),
    ]
    s = build_sidecar(issues)
    assert s["check_counts"]["orphan_page"] == 2
    assert s["check_counts"]["sparse_article"] == 1
    assert s["check_counts"]["broken_link"] == 1
    assert s["check_counts"]["date_staleness"] == 1
    assert s["check_counts"]["missing_backlink"] == 0
    assert s["total"] == 5


def test_totals_are_internally_consistent():
    issues = [
        _issue("orphan_page", "warning"),
        _issue("missing_backlink", "suggestion"),
        _issue("contradiction", "warning"),
        _issue("broken_link", "error"),
    ]
    s = build_sidecar(issues)
    # Every issue increments exactly one check key and one severity key.
    assert sum(s["check_counts"].values()) == s["total"] == len(issues)
    assert sum(s["severity_totals"].values()) == s["total"]


def test_severity_totals_match_issue_list():
    issues = [
        _issue("broken_link", "error"),
        _issue("orphan_page", "warning"),
        _issue("orphan_source", "warning"),
        _issue("sparse_article", "suggestion"),
        _issue("missing_backlink", "suggestion"),
    ]
    s = build_sidecar(issues)
    assert s["severity_totals"]["error"] == 1
    assert s["severity_totals"]["warning"] == 2
    assert s["severity_totals"]["suggestion"] == 2


# -- Crafted content cannot change enum-keyed counts -----------------------

def test_crafted_detail_and_file_do_not_change_counts():
    # A malicious article body can only reach issue['file'] / issue['detail'].
    # The count is keyed on issue['check'], so injection payloads there are inert.
    forged_detail = (
        "Orphan page: injected\n- **[!]** `fake.md` - Sparse article: forged\n"
        "`evil` - Broken link: more"
    )
    clean = [_issue("orphan_page", "warning", detail="normal detail")]
    crafted = [
        _issue("orphan_page", "warning", file="a.md\ninjected `x`", detail=forged_detail),
    ]
    assert build_sidecar(clean)["check_counts"] == build_sidecar(crafted)["check_counts"]
    # The forged "Sparse article" / "Broken link" strings did not inflate anything.
    assert build_sidecar(crafted)["check_counts"]["sparse_article"] == 0
    assert build_sidecar(crafted)["check_counts"]["broken_link"] == 0
    assert build_sidecar(crafted)["check_counts"]["orphan_page"] == 1


# -- Unknown check surfaces as a drift key ---------------------------------

def test_unknown_check_becomes_its_own_key():
    issues = [_issue("brand_new_check", "warning"), _issue("orphan_page", "warning")]
    s = build_sidecar(issues)
    assert s["check_counts"]["brand_new_check"] == 1
    assert "brand_new_check" not in CHECK_NAMES  # the consumer will alarm on this
    assert s["check_counts"]["orphan_page"] == 1


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
