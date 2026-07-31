#!/usr/bin/env python3
"""Plan and score the locked RP1 workflow campaign.

This module is deliberately side-effect free.  It prepares the inputs needed by
the later SMTP/remote-DB runner and scores captured evidence, but it never opens
a network connection or mutates a workflow database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ROLEPLAY_DIR = ROOT / "specs/2026-07-29-allied-carbon-agent/roleplay"
TEMPLATE_PATH = ROOT / "tests/fixtures/workflow/synthetic_allied_like.yaml"
LOCKED_FIXTURES = {
    "arcs-01-04.json": "10f969dddd9841ca5c51506b8e961dcda5fd47ea116394b61bcb52bf5f0f93bd",
    "arcs-05-08.json": "ba62f24d846e05825595650e8b4aecf86d0b79eebc19f5fdaa9977483b21bde7",
    "arcs-09-12.json": "371ca962d746aedff77bbc429121e1fc682fe97dee75e88f41de390a3839cd5f",
}
EXPECTED_ARCS = 12
EXPECTED_EMAILS = 25

# These expectations cannot be proven from the current persisted evidence.
EVIDENCE_LIMITS = (
    "correlation.candidate_count",
    "correlation.compatible_candidate_count",
    "correlation.candidates_rejected",
    "correlation.selection_reason",
    "correlation.position",
    "correlation.reason",
    "correlation.usable_discriminator_count",
    "agent_action.review.options",
)
REFERENCE_KEYS = (
    "in_reply_to_message_id",
    "thread_parent_message_id",
    "forwarded_original_message_id",
    "forwarded_from_message_id",
    "duplicate_of_message_id",
)


class CampaignContractError(ValueError):
    """The locked campaign or supplied evidence violates its contract."""


@dataclass(frozen=True)
class LockedCampaign:
    documents: tuple[dict[str, Any], ...]
    arcs: tuple[dict[str, Any], ...]
    fixture_hashes: dict[str, str]

    @property
    def email_count(self) -> int:
        return sum(len(arc["emails"]) for arc in self.arcs)

    @property
    def probe_count(self) -> int:
        return sum(len(arc.get("state_probes", [])) for arc in self.arcs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def load_locked_campaign(roleplay_dir: Path = ROLEPLAY_DIR) -> LockedCampaign:
    """Load RP1 only when all locked fixture bytes and population gates match."""
    documents: list[dict[str, Any]] = []
    arcs: list[dict[str, Any]] = []
    seen_messages: set[str] = set()
    observed_hashes: dict[str, str] = {}

    for filename, expected_hash in LOCKED_FIXTURES.items():
        path = roleplay_dir / filename
        if not path.is_file():
            raise CampaignContractError(f"locked fixture missing: {path}")
        observed_hash = _sha256(path)
        observed_hashes[filename] = observed_hash
        if observed_hash != expected_hash:
            raise CampaignContractError(
                f"locked fixture hash mismatch for {filename}: "
                f"expected {expected_hash}, observed {observed_hash}"
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("campaign") != "rp1":
            raise CampaignContractError(f"{filename}: campaign must be rp1")
        if document.get("answer_key_locked_before_execution") is not True:
            raise CampaignContractError(f"{filename}: answer key is not locked")
        if not isinstance(document.get("arcs"), list):
            raise CampaignContractError(f"{filename}: arcs must be a list")
        documents.append(document)
        arcs.extend(document["arcs"])

    expected_arc_ids = {f"RP1-A{number:02d}" for number in range(1, 13)}
    observed_arc_ids = {arc.get("id") for arc in arcs}
    if len(arcs) != EXPECTED_ARCS or observed_arc_ids != expected_arc_ids:
        raise CampaignContractError(
            f"RP1 population must be {EXPECTED_ARCS} unique arcs"
        )
    for arc in arcs:
        if not isinstance(arc.get("expected_final"), dict):
            raise CampaignContractError(f"{arc['id']}: expected_final missing")
        for email in arc.get("emails", []):
            message_id = email.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                raise CampaignContractError(f"{arc['id']}: invalid message_id")
            if message_id in seen_messages:
                raise CampaignContractError(f"duplicate logical message_id: {message_id}")
            seen_messages.add(message_id)
            if not isinstance(email.get("answer_key"), dict):
                raise CampaignContractError(
                    f"{arc['id']} {message_id}: answer_key missing"
                )

    email_count = sum(len(arc.get("emails", [])) for arc in arcs)
    if email_count != EXPECTED_EMAILS:
        raise CampaignContractError(
            f"RP1 population must be {EXPECTED_EMAILS} emails, observed {email_count}"
        )
    return LockedCampaign(tuple(documents), tuple(arcs), observed_hashes)


def logical_to_wire_message_id(logical_id: str) -> str:
    """Normalize a fixture ID for SMTP while retaining the logical ID separately."""
    if not isinstance(logical_id, str) or not logical_id:
        raise CampaignContractError("message_id must be a non-empty string")
    if "\r" in logical_id or "\n" in logical_id or any(ch.isspace() for ch in logical_id):
        raise CampaignContractError(f"unsafe message_id: {logical_id!r}")
    bracketed = logical_id.startswith("<") or logical_id.endswith(">")
    if bracketed and not (logical_id.startswith("<") and logical_id.endswith(">")):
        raise CampaignContractError(f"malformed message_id: {logical_id!r}")
    inner = logical_id[1:-1] if bracketed else logical_id
    if not inner or "<" in inner or ">" in inner or "@" not in inner:
        raise CampaignContractError(f"malformed message_id: {logical_id!r}")
    return logical_id if bracketed else f"<{logical_id}>"


def evidence_citation(
    table: str, identity: dict[str, Any], query: str, observed: Any
) -> dict[str, Any]:
    """Return the P5a evidence citation shape."""
    if not table or not query or not isinstance(identity, dict):
        raise CampaignContractError("citation requires table, identity, and query")
    return {
        "table": table,
        "identity": _json_clone(identity),
        "query": query,
        "observed": _json_clone(observed),
    }


def _reference_headers(answer_key: dict[str, Any]) -> dict[str, str]:
    corr = answer_key.get("corr", {})
    if not isinstance(corr, dict):
        return {}
    references: list[str] = []
    for key in REFERENCE_KEYS:
        value = corr.get(key)
        if isinstance(value, str):
            wire = logical_to_wire_message_id(value)
            if wire not in references:
                references.append(wire)
    if not references:
        return {}
    return {
        "in_reply_to": references[-1],
        "references": " ".join(references),
    }


def _canonical_alias(instance: dict[str, Any]) -> str:
    entity_key = instance.get("entity_key")
    if not isinstance(entity_key, str) or not entity_key:
        raise CampaignContractError("initial instance must have entity_key")
    return entity_key


def build_campaign_plan(
    campaign: LockedCampaign,
    *,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 587,
    smtp_user_env: str = "E1_STAGING_MAIL_USER",
    smtp_password_env: str = "E1_DORM1_APP_PASSWORD",
    recipient: str = "allied-workflow-staging",
    remote_db: str = "pa-workflow-dev",
    worker_profile: str = "dorm1",
) -> dict[str, Any]:
    """Build a deterministic, mutation-free orchestration plan."""
    planned_arcs: list[dict[str, Any]] = []
    for arc in campaign.arcs:
        seeds = []
        for instance in arc["initial_instances"]:
            seeds.append(
                {
                    "canonical_alias": _canonical_alias(instance),
                    "entity_key": _canonical_alias(instance),
                    "corr": _json_clone(instance.get("corr", {})),
                    "vars": _json_clone(instance.get("vars", {})),
                    "requested_step": instance["step"],
                    "operations": [
                        "wf_engine.create_instance",
                        "wf_engine.ingest_event(source=campaign_seed)",
                        "wf_engine.advance(to_step=requested_step) when needed",
                    ],
                }
            )
        emails = []
        for email in arc["emails"]:
            locked_body = email["body"]
            sender = email["from_display"]
            wire_body = f"{locked_body}\n\n--\n{sender} <dorm1@staging.invalid>\n"
            emails.append(
                {
                    "step": email["step"],
                    "logical_message_id": email["message_id"],
                    "wire_message_id": logical_to_wire_message_id(
                        email["message_id"]
                    ),
                    "from_display": sender,
                    "from_address_env": smtp_user_env,
                    "recipient": recipient,
                    "subject": email["subject"],
                    "locked_body": locked_body,
                    "wire_body": wire_body,
                    "locked_body_sha256": hashlib.sha256(
                        locked_body.encode("utf-8")
                    ).hexdigest(),
                    "wire_body_sha256": hashlib.sha256(
                        wire_body.encode("utf-8")
                    ).hexdigest(),
                    "headers": _reference_headers(email["answer_key"]),
                    "settle_before_next": True,
                }
            )
        planned_arcs.append(
            {
                "id": arc["id"],
                "seed_plan": seeds,
                "emails": emails,
                "state_probes": [
                    {
                        "step": probe["step"],
                        "after_email_step": probe["after_email_step"],
                        "source": probe["source"],
                        "entity_key": probe["entity_key"],
                        "request": _json_clone(probe["request"]),
                        "result": _json_clone(probe["result"]),
                        "expected": _json_clone(probe["expected"]),
                    }
                    for probe in sorted(
                        arc.get("state_probes", []),
                        key=lambda item: (item["after_email_step"], item["step"]),
                    )
                ],
                "expected_final": _json_clone(arc["expected_final"]),
            }
        )

    return {
        "campaign": "rp1",
        "mode": "plan-only",
        "network_performed": False,
        "database_mutated": False,
        "population": {
            "arcs": len(campaign.arcs),
            "emails": campaign.email_count,
            "probes": campaign.probe_count,
        },
        "locked_fixture_hashes": campaign.fixture_hashes,
        "workflow_template": {
            "path": str(TEMPLATE_PATH.relative_to(ROOT)),
            "sha256": _sha256(TEMPLATE_PATH),
            "mutation": "unchanged",
        },
        "orchestration": {
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_user_env": smtp_user_env,
            "smtp_password_env": smtp_password_env,
            "recipient": recipient,
            "remote_db": remote_db,
            "worker_profile": worker_profile,
            "poll_interval_seconds": 2,
            "ingress_timeout_seconds": 180,
            "worker_timeout_seconds": 600,
        },
        "citation_contract": {
            "format": ["table", "identity", "query", "observed"],
            "style": "P5a",
        },
        "evidence_limited_paths": list(EVIDENCE_LIMITS),
        "arcs": planned_arcs,
    }


def _is_limited(path: str, expected: Any) -> bool:
    if path in EVIDENCE_LIMITS:
        return True
    # A list-valued target describes an ambiguous candidate set, which is not
    # persisted by the current evidence plane.
    return path == "correlation.target" and isinstance(expected, list)


def compare_subset(
    expected: Any, observed: Any, path: str = ""
) -> dict[str, list[dict[str, Any]]]:
    """Compare only answer-key fields, retaining evidence-limit distinctions."""
    result: dict[str, list[dict[str, Any]]] = {
        "matched": [],
        "failed": [],
        "unobservable": [],
    }
    if isinstance(expected, dict):
        observed_dict = observed if isinstance(observed, dict) else {}
        for key, value in expected.items():
            child = f"{path}.{key}" if path else key
            if key not in observed_dict:
                bucket = "unobservable" if _is_limited(child, value) else "failed"
                result[bucket].append(
                    {"path": child, "expected": _json_clone(value), "observed": None}
                )
                continue
            nested = compare_subset(value, observed_dict[key], child)
            for bucket in result:
                result[bucket].extend(nested[bucket])
        return result
    if expected == observed:
        result["matched"].append(
            {"path": path, "expected": _json_clone(expected), "observed": _json_clone(observed)}
        )
    else:
        result["failed"].append(
            {"path": path, "expected": _json_clone(expected), "observed": _json_clone(observed)}
        )
    return result


def _status(result: dict[str, list[dict[str, Any]]]) -> str:
    if result["failed"]:
        return "fail"
    if result["unobservable"]:
        return "evidence-limited"
    return "pass"


def score_answer_key(
    answer_key: dict[str, Any], observed: dict[str, Any]
) -> dict[str, Any]:
    extraction_expected = {
        key: answer_key[key] for key in ("event_type", "payload", "corr") if key in answer_key
    }
    sections = {
        "extraction": compare_subset(extraction_expected, observed),
        "correlation": compare_subset(
            answer_key.get("correlation", {}),
            observed.get("correlation", {}),
            "correlation",
        ),
        "action": compare_subset(
            answer_key.get("agent_action", {}),
            observed.get("agent_action", {}),
            "agent_action",
        ),
    }
    statuses = {name: _status(value) for name, value in sections.items()}
    overall = "fail" if "fail" in statuses.values() else (
        "evidence-limited" if "evidence-limited" in statuses.values() else "pass"
    )
    return {"status": overall, "section_status": statuses, "sections": sections}


def score_campaign(
    campaign: LockedCampaign, observed: dict[str, Any]
) -> dict[str, Any]:
    observed_arcs = observed.get("arcs", {})
    scored_arcs: list[dict[str, Any]] = []
    email_counts = {"pass": 0, "evidence-limited": 0, "fail": 0}
    probe_counts = {"pass": 0, "evidence-limited": 0, "fail": 0}
    arc_counts = {"pass": 0, "evidence-limited": 0, "fail": 0}
    for arc in campaign.arcs:
        arc_observed = observed_arcs.get(arc["id"], {})
        observed_emails = arc_observed.get("emails", {})
        email_results = []
        for email in arc["emails"]:
            logical_id = email["message_id"]
            result = score_answer_key(
                email["answer_key"], observed_emails.get(logical_id, {})
            )
            email_counts[result["status"]] += 1
            email_results.append({"logical_message_id": logical_id, **result})
        probe_results = []
        observed_probes = arc_observed.get("state_probes", [])
        for index, probe in enumerate(
            sorted(
                arc.get("state_probes", []),
                key=lambda item: (item["after_email_step"], item["step"]),
            )
        ):
            probe_observed = (
                observed_probes[index]
                if isinstance(observed_probes, list) and index < len(observed_probes)
                else {}
            )
            comparison = compare_subset(
                probe["expected"], probe_observed, f"state_probes[{index}].expected"
            )
            probe_status = _status(comparison)
            probe_counts[probe_status] += 1
            probe_results.append(
                {
                    "step": probe["step"],
                    "after_email_step": probe["after_email_step"],
                    "status": probe_status,
                    "comparison": comparison,
                }
            )
        final_result = compare_subset(
            arc["expected_final"], arc_observed.get("expected_final", {}), "expected_final"
        )
        final_status = _status(final_result)
        arc_counts[final_status] += 1
        scored_arcs.append(
            {
                "id": arc["id"],
                "emails": email_results,
                "state_probes": probe_results,
                "expected_final": {
                    "status": final_status,
                    "comparison": final_result,
                },
            }
        )
    verdict = (
        "fail"
        if email_counts["fail"] or probe_counts["fail"] or arc_counts["fail"]
        else (
            "evidence-limited"
            if (
                email_counts["evidence-limited"]
                or probe_counts["evidence-limited"]
                or arc_counts["evidence-limited"]
            )
            else "pass"
        )
    )
    return {
        "campaign": "rp1",
        "population": {
            "arcs": len(campaign.arcs),
            "emails": campaign.email_count,
            "probes": campaign.probe_count,
        },
        "denominators": {
            "email_answer_keys": campaign.email_count,
            "arc_expected_final": len(campaign.arcs),
            "state_probes": campaign.probe_count,
        },
        "email_counts": email_counts,
        "probe_counts": probe_counts,
        "arc_counts": arc_counts,
        "verdict": verdict,
        "status": verdict,
        "arcs": scored_arcs,
    }


def _write_json(value: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(rendered, end="")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "score"), default="plan")
    parser.add_argument("--roleplay-dir", type=Path, default=ROLEPLAY_DIR)
    parser.add_argument("--observed-path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--smtp-host", default="smtp.gmail.com")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--smtp-user-env", default="E1_STAGING_MAIL_USER")
    parser.add_argument("--smtp-password-env", default="E1_DORM1_APP_PASSWORD")
    parser.add_argument("--recipient", default="allied-workflow-staging")
    parser.add_argument("--remote-db", default="pa-workflow-dev")
    parser.add_argument("--worker-profile", default="dorm1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    campaign = load_locked_campaign(args.roleplay_dir)
    if args.mode == "score":
        if args.observed_path is None:
            raise SystemExit("--observed-path is required with --mode score")
        observed = json.loads(args.observed_path.read_text(encoding="utf-8"))
        result = score_campaign(campaign, observed)
    else:
        result = build_campaign_plan(
            campaign,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            smtp_user_env=args.smtp_user_env,
            smtp_password_env=args.smtp_password_env,
            recipient=args.recipient,
            remote_db=args.remote_db,
            worker_profile=args.worker_profile,
        )
    _write_json(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
