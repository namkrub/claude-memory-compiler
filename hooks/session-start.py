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
    except Exception:
        # Fixed static string only. Never str(exc), never traceback.format_exc(),
        # never a path or filename pulled from the exception object.
        return f"## Knowledge Index\n\n{STALE_INDEX_WARNING}"


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
