"""Static contract tests for the workflow dashboard bundle."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "workflow" / "dashboard"
SCRIPT = PLUGIN / "dist" / "index.js"
STYLE = PLUGIN / "dist" / "style.css"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_workflow_dashboard_registers_with_one_tab():
    manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "workflow"
    assert manifest["tab"]["path"] == "/workflow"
    assert manifest["entry"] == "dist/index.js"
    assert 'register("workflow", WorkflowPage)' in _script()


def test_board_is_collection_bound_and_detail_uses_timeline():
    source = _script()
    assert 'fetchJSON(API + "/board")' in source
    assert '"/instances/" + encodeURIComponent(id) + "/timeline"' in source
    assert source.count('fetchJSON(API + "/board")') == 1
    assert "function WorkflowCard(props)" in source
    card_source = source.split("function WorkflowCard(props)", 1)[1].split(
        "function StageColumn(props)", 1
    )[0]
    assert "fetchJSON" not in card_source
    assert "TimelineSkeleton" in source
    assert "No transitions recorded." in source
    assert "props.onClose" in source
    assert "item.stages" in source  # U3 collection schema


def test_graph_uses_host_react_flow_without_modeling_library():
    source = _script()
    package = (ROOT / "web" / "package.json").read_text(encoding="utf-8")
    assert "flow.ReactFlow" in source
    assert "flow.ReactFlowProvider" in source
    assert "advance_to" in source
    assert "waits" in source
    assert "nodesDraggable: false" in source
    assert "nodesConnectable: false" in source
    assert '"@xyflow/react": ">=12.11.2 <13"' in package
    combined = source + package
    assert "bpmn-js" not in combined
    assert "react-kanban" not in combined
    assert "kanban-board" not in combined


def test_actions_validate_edits_refresh_and_do_not_render_tokens():
    source = _script()
    assert "function parseObject(value)" in source
    assert 'submit("approved")' in source
    assert 'submit("edited_approved")' in source
    assert 'submit("rejected")' in source
    assert "props.onChanged()" in source
    assert 'type: "password"' in source
    assert "resume_token" not in source


def test_mobile_board_and_responsive_graph_rules_are_present():
    css = STYLE.read_text(encoding="utf-8")
    assert "overflow-x: auto" in css
    assert "@media (max-width: 600px)" in css
    assert "calc(100vw - 2.2rem)" in css
    assert ".hermes-workflow-graph" in css
