"""Path constants and configuration for the personal knowledge base."""

from pathlib import Path
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────────────
# Tool lives inside the Bureau workspace; content (daily logs, knowledge)
# lives at Bureau/memory/ so it's treated as workspace content, not tool
# artifacts. Obsidian indexes both locations within the vault.
ROOT_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = ROOT_DIR.parent.parent.parent  # noel-workspace/
MEMORY_ROOT = WORKSPACE_ROOT / "Bureau" / "memory"

DAILY_DIR = MEMORY_ROOT / "daily"
KNOWLEDGE_DIR = MEMORY_ROOT / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"
QA_DIR = KNOWLEDGE_DIR / "qa"
REPORTS_DIR = WORKSPACE_ROOT / "Bureau" / "melfi" / "lint-reports"
SCRIPTS_DIR = ROOT_DIR / "scripts"
HOOKS_DIR = ROOT_DIR / "hooks"
AGENTS_FILE = ROOT_DIR / "AGENTS.md"

INDEX_FILE = KNOWLEDGE_DIR / "index.md"
LOG_FILE = KNOWLEDGE_DIR / "log.md"
STATE_FILE = SCRIPTS_DIR / "state.json"

# Personal recall memory (Claude Code auto-memory). Lives OUTSIDE the workspace
# and is gitignored, but loads into every session, so it drifts silently with no
# version control. The lint scans it for date-staleness (past dates written as
# pending commitments) even though it is not part of the Bureau wiki.
PERSONAL_MEMORY_DIR = (
    Path.home()
    / ".claude"
    / "projects"
    / "-Users-noelburkman-noel-workspace"
    / "memory"
)

# ── Timezone ───────────────────────────────────────────────────────────
TIMEZONE = "America/Los_Angeles"


def now_iso() -> str:
    """Current time in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    """Current date in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
