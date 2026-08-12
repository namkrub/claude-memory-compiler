"""Tests for workspace-routing checks in the live lint producer."""

from __future__ import annotations

from pathlib import Path

from lint import (
    CHECK_NAMES,
    build_sidecar,
    check_agent_registry,
    check_router_render_drift,
    check_workspace_routes,
    check_workspace_write_notices,
    structural_checks,
    generate_report,
)


def _write_fake_routing_module(workspace_root: Path) -> None:
    tools = workspace_root / "Bureau" / "tools"
    tools.mkdir(parents=True)
    (tools / "workspace_routing.py").write_text(
        """
def lint_issues(manifest_path, workspace_root):
    return [{
        "severity": "error",
        "check": "workspace_routing",
        "file": str(manifest_path),
        "detail": "Workspace route drift: synthetic finding.",
    }]

def notice_backstop_issues(manifest_path, workspace_root, mailbox_root):
    return [{
        "severity": "warning",
        "check": "workspace_routing",
        "file": str(mailbox_root),
        "detail": "Workspace route drift: synthetic missing notice.",
    }]
""".lstrip(),
        encoding="utf-8",
    )


def test_workspace_routing_is_known_check():
    assert "workspace_routing" in CHECK_NAMES


def test_live_lint_registers_all_routing_checks():
    names = {name for name, _ in structural_checks()}
    assert "Workspace routes" in names
    assert "Workspace write notices" in names
    assert "Workspace router render drift" in names
    assert "Agent registry" in names


def test_agent_registry_errors_flow_into_workspace_routing_check(tmp_path: Path):
    tools = tmp_path / "Bureau" / "tools"
    tools.mkdir(parents=True)
    (tools / "agent_registry.py").write_text(
        "from collections import namedtuple\n"
        "Finding = namedtuple('Finding', 'severity code slug detail')\n"
        "def validate_agent_surfaces(root):\n"
        "    return [Finding('error', 'codex-runtime-missing', 'fixture', 'gone')]\n",
        encoding="utf-8",
    )
    issues = check_agent_registry(tmp_path)
    assert issues == [{
        "severity": "error",
        "check": "workspace_routing",
        "file": str(tmp_path / "Bureau" / "Team" / "active-agents.json"),
        "detail": "Workspace route drift: [codex-runtime-missing] fixture: gone.",
    }]


def test_workspace_route_findings_flow_into_sidecar(tmp_path: Path):
    _write_fake_routing_module(tmp_path)
    issues = check_workspace_routes(tmp_path)
    sidecar = build_sidecar(issues)
    assert len(issues) == 1
    assert sidecar["check_counts"]["workspace_routing"] == 1


def test_sidecar_binds_to_requested_run_id():
    sidecar = build_sidecar([], run_id="agent-run-42")
    assert sidecar["run_id"] == "agent-run-42"


def test_workspace_write_notice_adapter_uses_review_mailbox(tmp_path: Path):
    _write_fake_routing_module(tmp_path)
    issues = check_workspace_write_notices(tmp_path)
    assert len(issues) == 1
    assert issues[0]["check"] == "workspace_routing"
    assert "review-mailbox" in issues[0]["file"]


def test_router_render_drift_reports_command_failure(tmp_path: Path):
    script = tmp_path / "Bureau" / "tools" / "render_workspace_router.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\nprint('synthetic drift')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    issues = check_router_render_drift(tmp_path)
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert issues[0]["check"] == "workspace_routing"
    assert "synthetic drift" in issues[0]["detail"]


def test_human_report_has_grouped_workspace_routing_section():
    issues = [
        {
            "severity": "error",
            "check": "workspace_routing",
            "file": "alpha/CONTEXT.md",
            "detail": "Workspace route drift: [missing-entry-file] alpha: missing.",
        },
        {
            "severity": "warning",
            "check": "workspace_routing",
            "file": "beta/spec/live.md",
            "detail": "Workspace route drift: [missing-write-notice] beta: missing.",
        },
    ]
    report = generate_report(issues)
    assert "## Workspace Routing" in report
    assert "### `missing-entry-file` (1)" in report
    assert "### `missing-write-notice` (1)" in report
    assert "alpha/CONTEXT.md" in report
