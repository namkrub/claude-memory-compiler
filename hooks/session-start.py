"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code starts a session,
this hook reads the knowledge base index and recent daily log, then injects
them as additional context so Claude always "remembers" what it has learned.

Configure in .claude/settings.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "command": "uv run python hooks/session-start.py"
        }]
    }
}

Knowledge retrieval (2026-07-27, Bureau/requirements/knowledge-retrieval-hook-swap-spec.md):
the full 143KB knowledge/index.md is no longer injected here -- it silently
truncated past MAX_CONTEXT_CHARS on every session. Instead this hook
re-ingests the knowledge graph (Bureau/db/load_notes_and_links.py, via its
non-blocking flock wrapper, skip-if-locked) and injects a slim header
(note/link counts + a query_knowledge.py usage line) in its place. Full
retrieval happens at query time: the session runs
`python3 Bureau/db/query_knowledge.py '<term>'` and Reads the returned
source path on demand. See reingest_and_build_header() below for the
failure-mode handling (Eugene review conditions 2 and 4).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import path config from scripts/config.py (patched to point at Bureau/memory/)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from config import DAILY_DIR, KNOWLEDGE_DIR, INDEX_FILE  # noqa: E402

# Also inject the workspace index.md (not just knowledge/index.md)
WORKSPACE_INDEX = ROOT.parent.parent.parent / "index.md"

# Shared cross-session state scratchpad
SESSION_STATE = ROOT.parent.parent / "session-state.md"

# Bureau/db -- the knowledge graph loader + connection helper live here.
# Imported lazily inside reingest_and_build_header() (not at module top
# level) so an import failure is caught by that function's own try/except
# rather than crashing session-start.py before the off-switch check runs.
DB_DIR = ROOT.parent.parent / "db"

MAX_CONTEXT_CHARS = 20_000
MAX_LOG_LINES = 30

# Fixed static string only -- Eugene condition 4. Never the exception repr,
# never a traceback, never a path derived from the exception. A malformed
# note or a locked/corrupt DB must not be able to push filesystem paths or
# partial file content into every session's context.
STALE_INDEX_WARNING = (
    "knowledge index may be stale; the last re-ingest attempt failed. "
    "Query with: python3 Bureau/db/query_knowledge.py '<term>' -- results "
    "reflect the most recent successful ingest, not necessarily this session."
)

# Guard 2 (2026-08-02). The bare except below returned STALE_INDEX_WARNING and
# nothing else, so a total ingest outage looked identical to a transient blip.
# A missing PyYAML declaration froze the index for 6 days without a single
# visible signal. Eugene condition 4 governs SESSION CONTEXT, not disk: the
# fixed string still goes to context, and the real reason goes here instead.
INGEST_ERROR_LOG = DB_DIR / "logs" / "knowledge-ingest-errors.log"
INGEST_ERROR_LOG_MAX_BYTES = 256 * 1024

# Guard 3 (2026-08-02). An undated warning cannot distinguish "failed once,
# 40 seconds ago" from "frozen since last Monday". Only a value matching this
# shape is ever interpolated into session context -- anything else falls back
# to the undated string, so arbitrary DB content can never reach context.
_ISO_DATE_PREFIX_LEN = 10  # YYYY-MM-DD


def _log_ingest_failure(exc: BaseException) -> None:
    """
    Append the real failure reason to a local log. Never raises: a failure to
    log must not escalate into a failure of session start. Rotates by simple
    truncation so an every-session failure cannot fill the disk.
    """
    try:
        import traceback

        INGEST_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if INGEST_ERROR_LOG.exists() and INGEST_ERROR_LOG.stat().st_size > INGEST_ERROR_LOG_MAX_BYTES:
            INGEST_ERROR_LOG.write_text("", encoding="utf-8")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with INGEST_ERROR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {stamp} SessionStart re-ingest failed ===\n")
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def _last_successful_ingest_date() -> str | None:
    """
    Read the date of the last successful ingest for the stale warning. Uses
    stdlib sqlite3 read-only and touches no Bureau module, so it still works
    when the failure being reported IS an import failure of those modules.
    Returns None on anything unexpected, including a value that does not look
    like an ISO date.
    """
    try:
        import sqlite3

        db_file = DB_DIR / "bureau.db"
        if not db_file.exists():
            return None
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT updated_at FROM knowledge_ingest_meta WHERE key = 'corpus_fingerprint'"
            ).fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        candidate = str(row[0])[:_ISO_DATE_PREFIX_LEN]
        datetime.strptime(candidate, "%Y-%m-%d")  # raises if not a real date
        return candidate
    except Exception:
        return None


def _stale_warning() -> str:
    """STALE_INDEX_WARNING, dated when the date is available and well-formed."""
    last = _last_successful_ingest_date()
    if not last:
        return STALE_INDEX_WARNING
    return (
        f"knowledge index may be stale; the last re-ingest attempt failed. "
        f"Last successful ingest: {last}. "
        "Query with: python3 Bureau/db/query_knowledge.py '<term>' -- results "
        "reflect that ingest, not necessarily this session."
    )


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def reingest_and_build_header() -> str:
    """
    Re-ingest the knowledge graph, then build the slim retrieval header
    that replaces the old 143KB full-index injection.

    Called ONLY from build_context(), which is called ONLY after the
    BUREAU_MEMORY=off early return in main(). This placement is load-bearing
    (Eugene condition 2): BUREAU_MEMORY=off must suppress this write, not
    just the injection. Moving this call above the off-check, or calling it
    at module import time, would silently break the kill switch.

    Any failure here -- DB missing/locked/corrupt, loader exception, import
    error, anything -- must never block session start and must never leak a
    path or exception repr into session context (both are prompt-injection
    / path-disclosure adjacent, since this text lands in every session).
    On any failure this returns the fixed STALE_INDEX_WARNING string and
    nothing else (Eugene condition 4).

    The re-ingest itself is non-blocking (Eugene condition 1a): it goes
    through load_notes_and_links.run_ingest_with_lock(), which acquires a
    non-blocking flock and skips immediately (returns, does not raise) if
    another ingest already holds it. Session start never waits on a lock.
    """
    try:
        sys.path.insert(0, str(DB_DIR))
        from db import get_db  # noqa: E402  (Bureau/db/db.py)
        import load_notes_and_links as loader  # noqa: E402

        conn = get_db()
        try:
            loader.run_ingest_with_lock(conn)
        finally:
            conn.close()

        conn = get_db()
        try:
            note_count = conn.execute(
                "SELECT COUNT(*) FROM knowledge_notes WHERE deleted_at IS NULL"
            ).fetchone()[0]
            link_count = conn.execute(
                "SELECT COUNT(*) FROM knowledge_note_links"
            ).fetchone()[0]
            last_ingest = conn.execute(
                "SELECT MAX(created_at) FROM knowledge_notes WHERE deleted_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()

        return (
            "## Knowledge Index\n\n"
            f"{note_count} notes, {link_count} links. Last ingest: {last_ingest}.\n"
            "Query with: python3 Bureau/db/query_knowledge.py '<term>'"
        )
    except Exception as exc:
        # Session context still gets fixed text only. Never str(exc), never
        # traceback.format_exc(), never a path or filename pulled from the
        # exception object. The one added value is a validated YYYY-MM-DD date
        # read from our own knowledge_ingest_meta row (guard 3).
        _log_ingest_failure(exc)
        return f"## Knowledge Index\n\n{_stale_warning()}"


def build_context() -> str:
    """Assemble the context to inject into the conversation."""
    parts = []

    # Today's date
    today = datetime.now(timezone.utc).astimezone()
    parts.append(f"## Today\n{today.strftime('%A, %B %d, %Y')}")

    # Workspace index.md (master content catalog) - injected first if present
    if WORKSPACE_INDEX.exists():
        workspace_content = WORKSPACE_INDEX.read_text(encoding="utf-8")
        parts.append(f"## Workspace Index\n\n{workspace_content}")

    # Cross-session state scratchpad
    if SESSION_STATE.exists():
        state_content = SESSION_STATE.read_text(encoding="utf-8")
        parts.append(f"## Active Session State\n\n{state_content}")

    # Knowledge index: slim retrieval header (re-ingest + counts), not the
    # full 143KB knowledge/index.md. See reingest_and_build_header().
    parts.append(reingest_and_build_header())

    # Recent daily log
    recent_log = get_recent_log()
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    context = "\n\n---\n\n".join(parts)

    # Truncate if too long
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def main():
    # Dev-session opt-out (2026-06-11, Eugene+Kade reviewed): BUREAU_MEMORY=off
    # suppresses the ~10K-token injection for THIS session only. We do NOT bare
    # exit (SessionStart has a JSON output contract, this file ends in
    # print(json.dumps(...))); instead we emit a loud one-line banner as the
    # context so a suppressed session announces itself and can never be mistaken
    # for a normal one. Default ON: any value other than "off" injects normally.
    #
    # This check must stay the FIRST thing main() does. build_context() (and
    # therefore reingest_and_build_header(), and therefore every knowledge-
    # graph DB write) is only reachable below this return. BUREAU_MEMORY=off
    # performs zero DB writes -- Eugene condition 2.
    if os.environ.get("BUREAU_MEMORY") == "off":
        banner = (
            "## Memory Suppressed (dev session)\n\n"
            "BUREAU_MEMORY=off: workspace index, knowledge base index, and "
            "end-of-session capture are all disabled for this session. Relaunch "
            "without the flag (plain `claude`) for knowledge work that should be "
            "remembered."
        )
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": banner,
            }
        }))
        return

    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
