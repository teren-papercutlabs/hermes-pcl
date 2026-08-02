"""Adapters and deterministic assertions for PA evaluation corpora.

The PA eval schema is intentionally richer than the bridge-message corpus used
by :class:`gateway.replay.ReplayPlan`.  This module keeps that richness in a
typed bundle while translating every user turn into a native replay plan.  A
multi-turn case is executed as consecutive plans sharing one replay namespace,
so Hermes' real session store carries the conversation between turns.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from gateway.replay import ReplayCorpus, ReplayPlan, canonical_digest


EXACT_KINDS = frozenset({"exact_present", "exact_absent"})
JUDGE_KINDS = frozenset({"must", "must_not"})
SUPPORTED_KINDS = EXACT_KINDS | JUDGE_KINDS


class PAEvalError(ValueError):
    """Raised when a PA evaluation corpus cannot be adapted safely."""


@dataclass(frozen=True)
class PAEvalExpectation:
    label: str
    kind: str
    text: str | None = None
    critical: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PAEvalExpectation":
        label = str(raw.get("label") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        if not label:
            raise PAEvalError("expectation label cannot be empty")
        if kind not in SUPPORTED_KINDS:
            raise PAEvalError(f"unsupported expectation kind {kind!r}")
        text = raw.get("text")
        if kind in EXACT_KINDS and (text is None or not str(text)):
            raise PAEvalError(f"{kind} expectation {label!r} requires text")
        return cls(
            label=label,
            kind=kind,
            text=str(text) if text is not None else None,
            critical=bool(raw.get("critical", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "text": self.text,
            "critical": self.critical,
        }


@dataclass(frozen=True)
class PAEvalTurn:
    text: str
    media: tuple[str, ...] = ()
    expected_before_next: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PAEvalTurn":
        text = str(raw.get("text") or "")
        if not text.strip():
            raise PAEvalError("turn text cannot be empty")
        media = tuple(str(item) for item in raw.get("media") or ())
        expected = tuple(
            str(item).strip()
            for item in raw.get("expected_before_next") or ()
            if str(item).strip()
        )
        return cls(text=text, media=media, expected_before_next=expected)


@dataclass(frozen=True)
class PAEvalCase:
    case_id: str
    tags: tuple[str, ...]
    turns: tuple[PAEvalTurn, ...]
    expected: tuple[PAEvalExpectation, ...]
    setup: str | None = None
    draws: int = 1
    canary: bool = False
    source: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PAEvalCase":
        case_id = str(raw.get("case_id") or "").strip()
        if not case_id:
            raise PAEvalError("case_id cannot be empty")
        input_data = raw.get("input")
        if not isinstance(input_data, Mapping):
            raise PAEvalError(f"{case_id}: input must be an object")
        turns_raw = input_data.get("turns")
        if not isinstance(turns_raw, list) or not turns_raw:
            raise PAEvalError(f"{case_id}: input.turns must be a non-empty array")
        expected_raw = raw.get("expected")
        if not isinstance(expected_raw, list) or not expected_raw:
            raise PAEvalError(f"{case_id}: expected must be a non-empty array")
        draws = int(raw.get("draws") or 1)
        if draws < 1:
            raise PAEvalError(f"{case_id}: draws must be at least 1")
        setup = input_data.get("setup")
        return cls(
            case_id=case_id,
            tags=tuple(str(tag) for tag in raw.get("tags") or ()),
            turns=tuple(PAEvalTurn.from_mapping(turn) for turn in turns_raw),
            expected=tuple(
                PAEvalExpectation.from_mapping(expectation)
                for expectation in expected_raw
            ),
            setup=str(setup).strip() if setup else None,
            draws=draws,
            canary=bool(raw.get("canary", False)),
            source=dict(raw.get("source") or {}),
            provenance=dict(raw.get("provenance") or {}),
        )


@dataclass(frozen=True)
class PAEvalCorpus:
    cases: tuple[PAEvalCase, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    source_digest: str = ""

    @classmethod
    def from_path(cls, path: str | Path) -> "PAEvalCorpus":
        source_path = Path(path).expanduser().resolve()
        raw = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise PAEvalError("PA eval corpus must be a JSON object")
        cases_raw = raw.get("cases")
        if not isinstance(cases_raw, list) or not cases_raw:
            raise PAEvalError("PA eval corpus must include a non-empty cases array")
        cases = tuple(PAEvalCase.from_mapping(case) for case in cases_raw)
        ids = [case.case_id for case in cases]
        if len(set(ids)) != len(ids):
            raise PAEvalError("PA eval corpus contains duplicate case_id values")
        return cls(
            cases=cases,
            meta=dict(raw.get("meta") or {}),
            source_path=str(source_path),
            source_digest=canonical_digest(raw),
        )

    def select(self, tags: Iterable[str] = ()) -> tuple[PAEvalCase, ...]:
        required = {str(tag).strip() for tag in tags if str(tag).strip()}
        if not required:
            return self.cases
        return tuple(case for case in self.cases if required.intersection(case.tags))


@dataclass(frozen=True)
class PAEvalTurnPlan:
    turn_index: int
    plan: ReplayPlan
    expectations: tuple[PAEvalExpectation, ...]


@dataclass(frozen=True)
class PAEvalReplayBundle:
    case: PAEvalCase
    run_id: str
    replay_namespace: str
    setup_plan: ReplayPlan | None
    turns: tuple[PAEvalTurnPlan, ...]


def _bridge_message(
    case: PAEvalCase,
    *,
    turn: PAEvalTurn,
    turn_index: int,
    chat_id: str,
    timestamp: int,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "messageId": f"{case.case_id}-turn-{turn_index + 1}",
        "chatId": chat_id,
        "chatType": "private",
        "isGroup": False,
        "senderId": "pa-eval-user",
        "senderName": "PA Eval",
        "body": turn.text,
        "timestamp": timestamp,
        "_pa_eval_case_id": case.case_id,
        "_pa_eval_turn_index": turn_index,
    }
    if turn.media:
        message.update({
            "hasMedia": True,
            "mediaUrls": list(turn.media),
        })
    return message


def _expectations_for_turn(
    case: PAEvalCase,
    turn: PAEvalTurn,
    turn_index: int,
) -> tuple[PAEvalExpectation, ...]:
    by_label = {expectation.label: expectation for expectation in case.expected}
    captured: list[PAEvalExpectation] = []
    for label in turn.expected_before_next:
        captured.append(
            by_label.get(label)
            or PAEvalExpectation(label=label, kind="must")
        )
    if turn_index == len(case.turns) - 1:
        seen = {(item.label, item.kind, item.text) for item in captured}
        captured.extend(
            item
            for item in case.expected
            if (item.label, item.kind, item.text) not in seen
        )
    return tuple(captured)


def adapt_case_to_replay(
    case: PAEvalCase,
    *,
    draw: int = 1,
    platform: str = "telegram",
    runtime_manifest: Mapping[str, Any] | None = None,
) -> PAEvalReplayBundle:
    """Translate one PA eval case into native per-turn ReplayPlans.

    Each plan shares a replay namespace.  Running them in order on one
    ``GatewayRunner`` therefore uses Hermes' real multi-turn session history;
    no turn text is concatenated into another turn.
    """
    if draw < 1:
        raise PAEvalError("draw must be at least 1")
    token = uuid.uuid4().hex[:10]
    run_id = f"pa-eval-{case.case_id}-d{draw}-{token}"
    namespace = f"agent:replay:{run_id}"
    chat_id = f"pa-eval-{case.case_id}-d{draw}"
    runtime = dict(runtime_manifest or {})

    def make_plan(
        message: Mapping[str, Any],
        *,
        attempt_suffix: str,
        manifest: Mapping[str, Any],
        safe_commands: Sequence[str] = (),
    ) -> ReplayPlan:
        corpus = ReplayCorpus.from_messages(
            [message],
            source_type="pa_eval_case",
            source_manifest={
                "case_id": case.case_id,
                "draw": draw,
                **dict(manifest),
            },
        )
        corpus_manifest = corpus.manifest()
        corpus_manifest.update(dict(manifest))
        return ReplayPlan(
            platform=platform,
            messages=corpus.messages,
            run_id=run_id,
            attempt_id=f"{run_id}-{attempt_suffix}",
            replay_namespace=namespace,
            replay_safe_commands=tuple(safe_commands),
            source_path=None,
            replay_policy=corpus.replay_policy_manifest(),
            corpus_manifest=corpus_manifest,
            target_descriptor_manifest={
                "kind": "non_live_pa_eval_runtime",
                "case_id": case.case_id,
                "draw": draw,
                **runtime,
            },
        )

    setup_plan = None
    if case.setup:
        if case.setup != "/new":
            raise PAEvalError(
                f"{case.case_id}: unsupported setup {case.setup!r}; only /new is replay-safe"
            )
        setup_plan = make_plan(
            {
                "messageId": f"{case.case_id}-setup",
                "chatId": chat_id,
                "chatType": "private",
                "isGroup": False,
                "senderId": "pa-eval-user",
                "senderName": "PA Eval",
                "body": case.setup,
                "timestamp": 1_800_000_000,
                "_pa_eval_case_id": case.case_id,
                "_pa_eval_setup": True,
            },
            attempt_suffix="setup",
            manifest={"phase": "setup"},
            safe_commands=("new",),
        )

    turn_plans: list[PAEvalTurnPlan] = []
    for turn_index, turn in enumerate(case.turns):
        expectations = _expectations_for_turn(case, turn, turn_index)
        message = _bridge_message(
            case,
            turn=turn,
            turn_index=turn_index,
            chat_id=chat_id,
            timestamp=1_800_000_100 + turn_index,
        )
        plan = make_plan(
            message,
            attempt_suffix=f"turn-{turn_index + 1}",
            manifest={
                "phase": "turn",
                "turn_index": turn_index,
                "turn_count": len(case.turns),
                "expectations": [item.to_dict() for item in expectations],
            },
        )
        turn_plans.append(
            PAEvalTurnPlan(
                turn_index=turn_index,
                plan=plan,
                expectations=expectations,
            )
        )
    return PAEvalReplayBundle(
        case=case,
        run_id=run_id,
        replay_namespace=namespace,
        setup_plan=setup_plan,
        turns=tuple(turn_plans),
    )


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_whitespace(value: str) -> str:
    """Collapse Unicode/Python whitespace while preserving case and wording."""
    return _WHITESPACE_RE.sub(" ", str(value)).strip()


def run_exact_assertion(
    response: str,
    expectation: PAEvalExpectation,
) -> dict[str, Any]:
    if expectation.kind not in EXACT_KINDS:
        raise PAEvalError(f"{expectation.kind} is not deterministic")
    normalized_response = normalize_whitespace(response)
    normalized_text = normalize_whitespace(expectation.text or "")
    present = normalized_text in normalized_response
    passed = present if expectation.kind == "exact_present" else not present
    return {
        **expectation.to_dict(),
        "status": "passed" if passed else "failed",
        "passed": passed,
        "normalized_text": normalized_text,
    }


def _final_outbound_text(
    outbound: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Return the last captured outbound body, not progress/interim chatter."""
    for index in range(len(outbound) - 1, -1, -1):
        entry = outbound[index]
        if not isinstance(entry, Mapping):
            continue
        kwargs = entry.get("kwargs")
        args = entry.get("args")
        content = kwargs.get("content") if isinstance(kwargs, Mapping) else None
        if content is None and isinstance(args, list) and len(args) >= 2:
            content = args[1]
        if content is not None:
            return str(content), {
                "outbound_index": index,
                "kind": str(entry.get("kind") or ""),
                "message_id": entry.get("message_id"),
            }
    return "", {
        "outbound_index": None,
        "kind": None,
        "message_id": None,
    }


async def run_replay_bundle(runner: Any, bundle: PAEvalReplayBundle) -> dict[str, Any]:
    """Run one adapted case and return per-turn assertion evidence."""
    setup_result = None
    if bundle.setup_plan is not None:
        setup_result = await runner.replay(bundle.setup_plan)

    turn_results: list[dict[str, Any]] = []
    for turn in bundle.turns:
        replay_result = await runner.replay(turn.plan)
        response, response_source = _final_outbound_text(replay_result.outbound)
        assertions: list[dict[str, Any]] = []
        for expectation in turn.expectations:
            if expectation.kind in EXACT_KINDS:
                assertions.append(run_exact_assertion(response, expectation))
            else:
                assertions.append({
                    **expectation.to_dict(),
                    "status": "pending_judge",
                    "passed": None,
                })
        turn_results.append({
            "turn_index": turn.turn_index,
            "attempt_id": replay_result.attempt_id,
            "processed": replay_result.processed,
            "response": response,
            "response_digest": canonical_digest(response),
            "response_source": response_source,
            "outbound_count": len(replay_result.outbound),
            "assertions": assertions,
        })

    deterministic = [
        assertion
        for turn in turn_results
        for assertion in turn["assertions"]
        if assertion["kind"] in EXACT_KINDS
    ]
    failed = [item for item in deterministic if not item["passed"]]
    return {
        "case_id": bundle.case.case_id,
        "tags": list(bundle.case.tags),
        "draw": int(bundle.run_id.split("-d", 1)[1].split("-", 1)[0]),
        "canary": bundle.case.canary,
        "run_id": bundle.run_id,
        "replay_namespace": bundle.replay_namespace,
        "setup": {
            "command": bundle.case.setup,
            "attempt_id": setup_result.attempt_id if setup_result else None,
            "blocked_commands": setup_result.blocked_commands if setup_result else [],
        },
        "turn_count": len(turn_results),
        "turns": turn_results,
        "deterministic": {
            "status": "passed" if deterministic and not failed else (
                "failed" if failed else "not_applicable"
            ),
            "assertion_count": len(deterministic),
            "passed": len(deterministic) - len(failed),
            "failed": len(failed),
        },
    }


async def run_pa_eval_corpus(
    corpus: PAEvalCorpus,
    *,
    runner: Any,
    tags: Iterable[str] = (),
    honor_draws: bool = False,
    platform: str = "telegram",
    runtime_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute selected cases through native Hermes replay."""
    selected = corpus.select(tags)
    cases: list[dict[str, Any]] = []
    for case in selected:
        draws = case.draws if honor_draws else 1
        for draw in range(1, draws + 1):
            bundle = adapt_case_to_replay(
                case,
                draw=draw,
                platform=platform,
                runtime_manifest=runtime_manifest,
            )
            cases.append(await run_replay_bundle(runner, bundle))

    deterministic = [item["deterministic"] for item in cases]
    return {
        "schema_version": 1,
        "corpus": {
            "source_path": corpus.source_path,
            "source_digest": corpus.source_digest,
            "declared_case_count": len(corpus.cases),
            "selected_case_count": len(selected),
            "tags": sorted({str(tag) for tag in tags if str(tag)}),
            "honor_draws": honor_draws,
        },
        "execution": {
            "platform": platform,
            "case_run_count": len(cases),
            "multi_turn_case_count": sum(1 for case in selected if len(case.turns) > 1),
            "turn_count": sum(item["turn_count"] for item in cases),
            "runtime": dict(runtime_manifest or {}),
        },
        "deterministic_summary": {
            "assertion_count": sum(item["assertion_count"] for item in deterministic),
            "passed": sum(item["passed"] for item in deterministic),
            "failed": sum(item["failed"] for item in deterministic),
            "case_runs_passed": sum(item["status"] == "passed" for item in deterministic),
            "case_runs_failed": sum(item["status"] == "failed" for item in deterministic),
            "case_runs_not_applicable": sum(
                item["status"] == "not_applicable" for item in deterministic
            ),
        },
        "cases": cases,
    }


def run_pa_eval_corpus_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_pa_eval_corpus(*args, **kwargs))
