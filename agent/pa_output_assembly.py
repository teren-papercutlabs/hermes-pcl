"""Deterministic, provenance-gated output assembly for PA responses.

The model owns only scope classification and marker placement.  Exact text is
loaded from approved typed artifacts and inserted after generation.  This keeps
verbatim compliance text out of the model's authorship surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from hermes_constants import get_hermes_home


class PAOutputAssemblyError(ValueError):
    """Raised when configured deterministic output assembly cannot be trusted."""


class PAOutputAssemblyRetry(PAOutputAssemblyError):
    """Raised when the model omitted a required scope or block marker."""

    def __init__(self, missing_markers: Sequence[str], detail: str) -> None:
        self.missing_markers = tuple(missing_markers)
        super().__init__(detail)


@dataclass(frozen=True)
class ComplianceBlock:
    block_id: str
    marker: str
    text: str
    required_tags: frozenset[str]
    exclusive_group: str | None
    artifact: str
    provenance: Mapping[str, Any]


_SCOPE_RE = re.compile(r"\[\[PA_SCOPE:([^\]]+)\]\]")
_HEADER_START = "# pa-source:"
_HEADER_END = "# ---"
_REQUIRED_PROVENANCE = ("approved_by", "approved_date", "ruling_ref", "status")


def output_assembly_policy(pa_context: Any) -> Mapping[str, Any]:
    brief = getattr(pa_context, "job_brief", None)
    response_policy = getattr(brief, "response_policy", None)
    if not isinstance(response_policy, Mapping):
        return {}
    policy = response_policy.get("output_assembly")
    return policy if isinstance(policy, Mapping) else {}


def output_assembly_enabled(pa_context: Any) -> bool:
    policy = output_assembly_policy(pa_context)
    return bool(policy.get("enabled", False)) and policy.get("mode", "marker") == "marker"


def output_assembly_max_attempts(pa_context: Any) -> int:
    policy = output_assembly_policy(pa_context)
    try:
        attempts = int(policy.get("max_attempts", 2))
    except (TypeError, ValueError):
        attempts = 2
    return max(1, min(attempts, 3))


def _parse_typed_artifact(path: Path, relative_path: str) -> list[ComplianceBlock]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != _HEADER_START:
        raise PAOutputAssemblyError(f"{relative_path}: missing {_HEADER_START!r}")
    metadata_lines: list[str] = []
    body_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line == _HEADER_END:
            body_index = index + 1
            break
        if not line.startswith("# "):
            raise PAOutputAssemblyError(
                f"{relative_path}: malformed provenance header line {index + 1}"
            )
        metadata_lines.append(line[2:])
    if body_index is None:
        raise PAOutputAssemblyError(f"{relative_path}: missing {_HEADER_END!r}")
    provenance = yaml.safe_load("\n".join(metadata_lines)) or {}
    if not isinstance(provenance, Mapping):
        raise PAOutputAssemblyError(f"{relative_path}: provenance must be a mapping")
    missing = [key for key in _REQUIRED_PROVENANCE if key not in provenance]
    if missing:
        raise PAOutputAssemblyError(
            f"{relative_path}: missing provenance fields: {', '.join(missing)}"
        )
    if provenance.get("status") != "approved":
        raise PAOutputAssemblyError(
            f"{relative_path}: deterministic output requires status=approved"
        )
    body = yaml.safe_load("\n".join(lines[body_index:])) or {}
    if not isinstance(body, Mapping) or body.get("schema_version") != 1:
        raise PAOutputAssemblyError(
            f"{relative_path}: expected compliance-block schema_version 1"
        )
    raw_blocks = body.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise PAOutputAssemblyError(f"{relative_path}: blocks must be non-empty")
    blocks: list[ComplianceBlock] = []
    for raw in raw_blocks:
        if not isinstance(raw, Mapping):
            raise PAOutputAssemblyError(f"{relative_path}: block must be a mapping")
        block_id = str(raw.get("id") or "").strip()
        marker = str(raw.get("marker") or "").strip()
        block_text = str(raw.get("text") or "").strip()
        tags = frozenset(
            str(tag).strip().upper()
            for tag in raw.get("required_tags") or ()
            if str(tag).strip()
        )
        exclusive_group = str(raw.get("exclusive_group") or "").strip() or None
        if not block_id or not marker or not block_text or not tags:
            raise PAOutputAssemblyError(
                f"{relative_path}: every block needs id, marker, text, required_tags"
            )
        if not marker.startswith("[[PA_BLOCK:") or not marker.endswith("]]" ):
            raise PAOutputAssemblyError(f"{relative_path}: invalid marker {marker!r}")
        blocks.append(
            ComplianceBlock(
                block_id=block_id,
                marker=marker,
                text=block_text,
                required_tags=tags,
                exclusive_group=exclusive_group,
                artifact=relative_path,
                provenance=dict(provenance),
            )
        )
    return blocks


def load_compliance_blocks(
    pa_context: Any,
    *,
    knowledge_root: str | Path | None = None,
) -> tuple[ComplianceBlock, ...]:
    policy = output_assembly_policy(pa_context)
    paths = policy.get("artifacts")
    if not isinstance(paths, list) or not paths:
        raise PAOutputAssemblyError("output_assembly.artifacts must be non-empty")
    root = Path(knowledge_root or (get_hermes_home() / "knowledge")).resolve()
    blocks: list[ComplianceBlock] = []
    seen_ids: set[str] = set()
    seen_markers: set[str] = set()
    for entry in paths:
        relative = Path(str(entry))
        if relative.is_absolute() or ".." in relative.parts:
            raise PAOutputAssemblyError(f"artifact path must be relative: {entry}")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PAOutputAssemblyError(f"artifact escapes knowledge root: {entry}") from exc
        if not path.is_file():
            raise PAOutputAssemblyError(f"approved artifact is missing: {entry}")
        for block in _parse_typed_artifact(path, relative.as_posix()):
            if block.block_id in seen_ids or block.marker in seen_markers:
                raise PAOutputAssemblyError(
                    f"duplicate compliance block id or marker: {block.block_id}"
                )
            seen_ids.add(block.block_id)
            seen_markers.add(block.marker)
            blocks.append(block)
    return tuple(blocks)


def render_output_assembly_skill(
    pa_context: Any,
    *,
    knowledge_root: str | Path | None = None,
) -> str:
    """Render the task-adjacent marker skill at the end of the PA job brief."""
    policy = output_assembly_policy(pa_context)
    if not policy.get("enabled", False) or policy.get("placement", "skill") != "skill":
        return ""
    return _render_output_instruction(pa_context, knowledge_root=knowledge_root, skill=True)


def render_output_assembly_constitution_instruction(
    pa_context: Any,
    *,
    knowledge_root: str | Path | None = None,
) -> str:
    policy = output_assembly_policy(pa_context)
    if not policy.get("enabled", False) or policy.get("placement", "skill") != "constitution":
        return ""
    return _render_output_instruction(pa_context, knowledge_root=knowledge_root, skill=False)


def _render_output_instruction(
    pa_context: Any,
    *,
    knowledge_root: str | Path | None,
    skill: bool,
) -> str:
    policy = output_assembly_policy(pa_context)
    blocks = load_compliance_blocks(pa_context, knowledge_root=knowledge_root)
    lines = [
        "# Output Compliance Skill" if skill else "## Output Compliance Instruction",
        (
            "This is the final instruction for this task. Follow it after composing the draft."
            if skill
            else "Apply this instruction while composing the draft."
        ),
    ]
    if policy.get("mode", "marker") == "verbatim":
        lines.append("Write each applicable protected block exactly as follows, without paraphrasing:")
        for block in blocks:
            tags = ",".join(sorted(block.required_tags))
            lines.append(f"- When scope includes {tags}: {block.text}")
    else:
        lines.extend(
            [
                "Emit exactly one scope marker: [[PA_SCOPE:NO_DRAFT]] when not returning a completed draft, or [[PA_SCOPE:DRAFT,<applicable tags>]] for a completed draft.",
                "Applicable tags include ROP, GENERAL_DISCLOSURES, PROTECTION_ALTERNATIVES, SUSTAINABILITY_NO, and SUSTAINABILITY_YES.",
            ]
        )
        lines.append(
            "Never type or paraphrase the protected text; emit its marker exactly where the block belongs."
        )
        for block in blocks:
            tags = ",".join(sorted(block.required_tags))
            lines.append(f"- When scope includes {tags}, emit {block.marker}.")
        lines.append(
            "A missing scope or required block marker makes the response fail closed and regenerate. Markers are removed by the runtime before delivery."
        )
    return "\n".join(lines)


def _scope_tags(response: str) -> tuple[frozenset[str], str]:
    matches = list(_SCOPE_RE.finditer(response))
    if len(matches) != 1:
        raise PAOutputAssemblyRetry(
            ("[[PA_SCOPE:...]]",),
            "response must contain exactly one PA scope marker",
        )
    tags = frozenset(
        item.strip().upper()
        for item in matches[0].group(1).split(",")
        if item.strip()
    )
    if not tags or not ({"DRAFT", "NO_DRAFT"} & tags):
        raise PAOutputAssemblyRetry(
            ("[[PA_SCOPE:DRAFT,...]] or [[PA_SCOPE:NO_DRAFT]]",),
            "scope marker must declare DRAFT or NO_DRAFT",
        )
    if "DRAFT" in tags and "NO_DRAFT" in tags:
        raise PAOutputAssemblyRetry(("one scope mode",), "scope cannot be both modes")
    stripped = response[: matches[0].start()] + response[matches[0].end() :]
    return tags, stripped


def assemble_pa_response(
    response: str,
    pa_context: Any,
    *,
    knowledge_root: str | Path | None = None,
    source_text: str = "",
) -> tuple[str, dict[str, Any]]:
    """Replace required markers with approved text or fail closed."""
    if not output_assembly_enabled(pa_context):
        return response, {"enabled": False, "inserted": []}
    blocks = load_compliance_blocks(pa_context, knowledge_root=knowledge_root)
    tags, assembled = _scope_tags(response)
    normalized_source = " ".join(source_text.lower().split())
    deterministic_scope: str | None = None
    sustainability_facts = list(
        re.finditer(
            r"do not exceed 50%|more than 50%|(?<!not )exceed 50%",
            normalized_source,
        )
    )
    if sustainability_facts:
        deterministic_scope = (
            "SUSTAINABILITY_NO"
            if sustainability_facts[-1].group(0).startswith("do not")
            else "SUSTAINABILITY_YES"
        )
    if deterministic_scope:
        tags = frozenset(
            (tags - {"SUSTAINABILITY_NO", "SUSTAINABILITY_YES"})
            | {deterministic_scope}
        )
    required = [block for block in blocks if block.required_tags.issubset(tags)]
    missing = [block.marker for block in required if assembled.count(block.marker) != 1]
    if missing:
        raise PAOutputAssemblyRetry(
            missing,
            "required deterministic compliance markers are missing or duplicated",
        )
    inserted: list[dict[str, Any]] = []
    required_groups = {
        block.exclusive_group for block in required if block.exclusive_group
    }
    for group in required_groups:
        selected = [block for block in required if block.exclusive_group == group]
        if len(selected) != 1:
            raise PAOutputAssemblyRetry(
                (f"one {group} scope tag",),
                f"scope selected {len(selected)} mutually-exclusive {group} blocks",
            )
        for alternative in blocks:
            if alternative.exclusive_group == group and alternative not in selected:
                assembled = assembled.replace(alternative.text, "")
    for block in blocks:
        count = assembled.count(block.marker)
        if block in required:
            assembled = assembled.replace(block.marker, block.text, 1)
            inserted.append(
                {
                    "id": block.block_id,
                    "artifact": block.artifact,
                    "approved_by": block.provenance.get("approved_by"),
                    "approved_date": block.provenance.get("approved_date"),
                    "ruling_ref": block.provenance.get("ruling_ref"),
                    "status": block.provenance.get("status"),
                }
            )
        elif count:
            assembled = assembled.replace(block.marker, "")
    if "[[PA_BLOCK:" in assembled or "[[PA_SCOPE:" in assembled:
        raise PAOutputAssemblyRetry(("no residual PA markers",), "unknown marker remains")
    return assembled.strip(), {
        "enabled": True,
        "scope_tags": sorted(tags),
        "deterministic_scope": deterministic_scope,
        "inserted": inserted,
    }


def retry_instruction(error: PAOutputAssemblyRetry) -> str:
    markers = ", ".join(error.missing_markers)
    return (
        "OUTPUT COMPLIANCE RETRY: the previous draft was withheld by the runtime. "
        f"Regenerate the entire answer and include exactly one scope marker plus: {markers}. "
        "Do not type the protected compliance wording yourself."
    )
