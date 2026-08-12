"""Tests for workspace-routing checks in the live lint producer."""

from __future__ import annotations

from pathlib import Path

from lint import (
    CHECK_NAMES,
    build_sidecar,
    check_router_render_drift,
    check_workspace_routes,
    check_workspace_write_notices,
    structural_checks,
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


def test_workspace_route_findings_flow_into_sidecar(tmp_path: Path):
    _write_fake_routing_module(tmp_path)
    issues = check_workspace_routes(tmp_path)
    sidecar = build_sidecar(issues)
    assert len(issues) == 1
    assert sidecar["check_counts"]["workspace_routing"] == 1


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

