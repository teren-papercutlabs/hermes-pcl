"""Focused tests for the durable TGG case-list coordinator terminal gate."""

from unittest.mock import Mock

from tools import delegate_tool
from tools.delegate_tool import _run_single_child


class _Parent:
    def __init__(self):
        self._active_children = []
        self._active_children_lock = None
        self._current_task_id = None

    def _touch_activity(self, _description):
        pass


class _CoordinatorChild:
    def __init__(self, turns, statuses):
        self._turns = iter(turns)
        self._statuses = iter(statuses)
        self.calls = []
        self.status_calls = []
        self._delegate_profile = "whatsapp_case_list_coordinator"
        self._delegate_context = "case_list_id: wa-list-20260821-01"
        self._delegate_name = None
        self._delegate_requested_role = None
        self._tgg_whatsapp_case_list_id = None
        self._credential_pool = None
        self._subagent_id = None
        self._delegate_depth = 1
        self._parent_subagent_id = None
        self._delegate_role = "leaf"
        self._delegate_saved_tool_names = []
        self.tool_progress_callback = None
        self.model = "test"
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_estimated_cost_usd = 0.0

    def get_activity_summary(self):
        return {"current_tool": None, "api_call_count": 1, "max_iterations": 50}

    def run_conversation(self, *, user_message, task_id):
        self.calls.append((user_message, task_id, id(self)))
        return next(self._turns)

    def _tgg_whatsapp_case_list_status(self, *, list_id):
        self.status_calls.append(list_id)
        return {"status": next(self._statuses)}

    def close(self):
        pass


def _terminal(text="turn complete"):
    return {
        "final_response": text,
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [],
    }


def test_coordinator_reuses_same_child_and_task_until_durable_list_complete():
    child = _CoordinatorChild(
        turns=[_terminal("first case submitted"), _terminal("all cases submitted")],
        statuses=["in_progress", "complete"],
    )

    result = _run_single_child(
        task_index=0,
        goal="Coordinate the list",
        child=child,
        parent_agent=_Parent(),
    )

    assert result["status"] == "completed"
    assert result["coordinator_list_status"] == "complete"
    assert result["coordinator_continuation_turns"] == 1
    assert len(child.calls) == 2
    assert {call[1] for call in child.calls} == {child.calls[0][1]}
    assert {call[2] for call in child.calls} == {id(child)}
    assert child.status_calls == ["wa-list-20260821-01", "wa-list-20260821-01"]
    assert "Continue the same durable" in child.calls[1][0]


def test_coordinator_stops_without_continuation_when_already_complete():
    child = _CoordinatorChild(turns=[_terminal()], statuses=["complete"])

    result = _run_single_child(
        task_index=0, goal="Coordinate the list", child=child, parent_agent=_Parent()
    )

    assert result["status"] == "completed"
    assert result["coordinator_continuation_turns"] == 0
    assert len(child.calls) == 1
    assert child.status_calls == ["wa-list-20260821-01"]


def test_coordinator_status_tool_failure_surfaces_incomplete_not_success():
    child = _CoordinatorChild(turns=[_terminal()], statuses=[])
    child._tgg_whatsapp_case_list_status = Mock(
        return_value={"error": "Systems status API unavailable"}
    )

    result = _run_single_child(
        task_index=0, goal="Coordinate the list", child=child, parent_agent=_Parent()
    )

    assert result["status"] == "failed"
    assert "cannot verify durable list" in result["error"]
    assert len(child.calls) == 1


def test_coordinator_visible_incomplete_cap_does_not_enqueue_a_duplicate_run(monkeypatch):
    monkeypatch.setattr(delegate_tool, "_WHATSAPP_CASE_LIST_CONTINUATION_CAP", 1)
    child = _CoordinatorChild(
        turns=[_terminal("first"), _terminal("second")],
        statuses=["in_progress", "in_progress"],
    )

    result = _run_single_child(
        task_index=0, goal="Coordinate the list", child=child, parent_agent=_Parent()
    )

    assert result["status"] == "failed"
    assert "remains visibly incomplete" in result["error"]
    assert result["coordinator_continuation_turns"] == 1
    # One original run plus one same-session continuation; no new list event.
    assert len(child.calls) == 2
    assert child.status_calls == ["wa-list-20260821-01", "wa-list-20260821-01"]


def test_non_coordinator_leaf_is_unaffected_and_never_queries_list_status():
    child = _CoordinatorChild(turns=[_terminal()], statuses=["in_progress"])
    child._delegate_profile = "ordinary_leaf"
    child._delegate_context = "case_list_id: wa-list-20260821-01"

    result = _run_single_child(
        task_index=0, goal="Investigate one case", child=child, parent_agent=_Parent()
    )

    assert result["status"] == "completed"
    assert len(child.calls) == 1
    assert child.status_calls == []
