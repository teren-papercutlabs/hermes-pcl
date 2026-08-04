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

    def __init__(
        self,
        missing_markers: Sequence[str],
        detail: str,
        *,
        instruction: str | None = None,
    ) -> None:
        self.missing_markers = tuple(missing_markers)
        #: Overrides the default marker-shaped retry note when the defect is
        #: not a missing marker (e.g. model-authored text standing in for a
        #: dropped approved block).
        self.instruction = instruction
        super().__init__(detail)


@dataclass(frozen=True)
class ComplianceSlot:
    """A deterministic hole in an approved sentence, filled from case state.

    The approved WORDING still lives whole in the typed artifact; a slot only
    names which recorded field supplies a value and which canonical tokens that
    field's answers map to.  The model never authors slot content, and an
    unmappable answer resolves to nothing rather than to a guess.
    """

    name: str
    field: str
    matches: tuple[tuple[re.Pattern[str], str], ...] = ()

    def resolve(self, raw_value: Any) -> str | None:
        if raw_value is None:
            return None
        text = str(raw_value).strip()
        if not text:
            return None
        if not self.matches:
            return text
        for pattern, value in self.matches:
            if pattern.search(text):
                return value
        return None


@dataclass(frozen=True)
class ComplianceBlock:
    block_id: str
    marker: str
    text: str
    required_tags: frozenset[str]
    exclusive_group: str | None
    artifact: str
    provenance: Mapping[str, Any]
    #: A selection-only block is never required by a model scope tag and is
    #: never shown to the model.  It exists to be substituted at another
    #: block's marker by deterministic, config-driven selection.
    selection_only: bool = False
    #: A repeatable block's approved text belongs once per component the draft
    #: covers, so its marker may legitimately appear more than once.  A
    #: non-repeatable block stays exactly-once: duplicating a general
    #: disclosure is itself a defect.
    repeatable: bool = False
    #: Slots resolved from case-record fields before insertion.
    slots: tuple[ComplianceSlot, ...] = ()
    #: Patterns that identify THIS block's subject matter in model-authored
    #: prose.  Drop-not-hole means an unresolvable slot removes the approved
    #: sentence; it must not mean the model's own version of that sentence
    #: ships in its place.  When the block is dropped and the draft still
    #: speaks to the topic in the model's words, assembly fails closed.
    #: The patterns are CLIENT vocabulary and live in the typed artifact.
    drop_guard: tuple[re.Pattern[str], ...] = ()

    def render(self, field_values: Mapping[str, Any] | None) -> str | None:
        """Return the approved text with every slot filled, or None."""
        if not self.slots:
            return self.text
        values = dict(field_values or {})
        rendered = self.text
        for slot in self.slots:
            resolved = slot.resolve(values.get(slot.field))
            if resolved is None:
                return None
            rendered = rendered.replace("{" + slot.name + "}", resolved)
        return rendered


_SCOPE_RE = re.compile(r"\[\[PA_SCOPE:([^\]]+)\]\]")
#: A block marker is the RUNTIME's token, not the model's prose — it must not
#: read as the model having written the block's topic.
_MARKER_RE = re.compile(r"\[\[PA_BLOCK:[^\]]+\]\]")
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


def _parse_slots(
    raw: Any,
    relative_path: str,
    block_id: str,
    block_text: str,
) -> tuple[ComplianceSlot, ...]:
    if raw in (None, (), []):
        if "{" in block_text:
            raise PAOutputAssemblyError(
                f"{relative_path}: block {block_id} has a slot placeholder but declares no slots"
            )
        return ()
    if not isinstance(raw, list) or not raw:
        raise PAOutputAssemblyError(f"{relative_path}: {block_id}.slots must be a non-empty list")
    slots: list[ComplianceSlot] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PAOutputAssemblyError(f"{relative_path}: {block_id}.slots entries must be mappings")
        name = str(item.get("name") or "").strip()
        field = str(item.get("field") or "").strip()
        if not name or not field:
            raise PAOutputAssemblyError(
                f"{relative_path}: {block_id}.slots entries need name and field"
            )
        placeholder = "{" + name + "}"
        if placeholder not in block_text:
            raise PAOutputAssemblyError(
                f"{relative_path}: {block_id} declares slot {name!r} that its text never uses"
            )
        matches: list[tuple[re.Pattern[str], str]] = []
        for match in item.get("matches") or ():
            if not isinstance(match, Mapping):
                raise PAOutputAssemblyError(
                    f"{relative_path}: {block_id}.slots.{name}.matches entries must be mappings"
                )
            pattern = str(match.get("pattern") or "")
            value = match.get("value")
            if not pattern or value is None:
                raise PAOutputAssemblyError(
                    f"{relative_path}: {block_id}.slots.{name}.matches needs pattern and value"
                )
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise PAOutputAssemblyError(
                    f"{relative_path}: {block_id}.slots.{name} bad pattern {pattern!r}: {exc}"
                ) from exc
            matches.append((compiled, str(value)))
        slots.append(ComplianceSlot(name=name, field=field, matches=tuple(matches)))
    declared = {"{" + slot.name + "}" for slot in slots}
    for found in re.findall(r"\{[a-zA-Z0-9_]+\}", block_text):
        if found not in declared:
            raise PAOutputAssemblyError(
                f"{relative_path}: {block_id} text uses undeclared slot {found}"
            )
    return tuple(slots)


def _parse_drop_guard(
    raw: Any,
    relative_path: str,
    block_id: str,
    has_slots: bool,
) -> tuple[re.Pattern[str], ...]:
    """Compile a block's model-authorship guard patterns.

    A guard only has meaning for a block that can be DROPPED at assembly
    time — today that is a slotted block whose recorded values may not
    resolve — so declaring one anywhere else is a config error rather than a
    silently inert setting.
    """
    if raw in (None, (), []):
        return ()
    if not isinstance(raw, list) or not raw:
        raise PAOutputAssemblyError(
            f"{relative_path}: {block_id}.drop_guard must be a non-empty list"
        )
    if not has_slots:
        raise PAOutputAssemblyError(
            f"{relative_path}: {block_id} declares drop_guard but has no slots to drop on"
        )
    patterns: list[re.Pattern[str]] = []
    for item in raw:
        pattern = str(item or "")
        if not pattern:
            raise PAOutputAssemblyError(
                f"{relative_path}: {block_id}.drop_guard entries must be non-empty patterns"
            )
        try:
            patterns.append(re.compile(pattern))
        except re.error as exc:
            raise PAOutputAssemblyError(
                f"{relative_path}: {block_id}.drop_guard bad pattern {pattern!r}: {exc}"
            ) from exc
    return tuple(patterns)


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
        slots = _parse_slots(raw.get("slots"), relative_path, block_id, block_text)
        drop_guard = _parse_drop_guard(
            raw.get("drop_guard"), relative_path, block_id, bool(slots)
        )
        blocks.append(
            ComplianceBlock(
                block_id=block_id,
                marker=marker,
                text=block_text,
                required_tags=tags,
                exclusive_group=exclusive_group,
                artifact=relative_path,
                provenance=dict(provenance),
                selection_only=bool(raw.get("selection_only", False)),
                repeatable=bool(raw.get("repeatable", False)),
                slots=slots,
                drop_guard=drop_guard,
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
            if block.selection_only:
                continue
            tags = ",".join(sorted(block.required_tags))
            lines.append(f"- When scope includes {tags}: {block.text}")
    else:
        runtime_tags = {
            str(tag).strip().upper()
            for tag in policy.get("runtime_scope_tags") or ()
            if str(tag).strip()
        }
        # The tag vocabulary is DERIVED from the approved artifacts, never
        # hard-coded here: a shared module must not carry one deployment's
        # compliance vocabulary, and a hand-maintained list silently omits the
        # tag a newly approved block needs.
        declarable = sorted(
            tag
            for block in blocks
            if not block.selection_only
            for tag in block.required_tags
            if tag not in {"DRAFT", "NO_DRAFT"} and tag not in runtime_tags
        )
        lines.append(
            "Emit exactly one scope marker: [[PA_SCOPE:NO_DRAFT]] when not returning a completed draft, or [[PA_SCOPE:DRAFT,<applicable tags>]] for a completed draft."
        )
        if declarable:
            lines.append(
                "Applicable tags: " + ", ".join(dict.fromkeys(declarable)) + "."
            )
        lines.append(
            "A tag that applies without a completed draft still belongs on the scope marker, e.g. [[PA_SCOPE:NO_DRAFT,<applicable tags>]]."
        )
        lines.append(
            "Never type or paraphrase the protected text; emit its marker exactly where the block belongs."
        )
        for block in blocks:
            if block.selection_only:
                continue
            tags = ",".join(sorted(block.required_tags))
            if block.repeatable:
                lines.append(
                    f"- When scope includes {tags}, emit {block.marker} once inside"
                    " EACH component it covers — repeat the marker per component"
                    " rather than hoisting one copy to the top."
                )
            elif runtime_tags & block.required_tags:
                # The runtime, not the model, resolves this block's scope from
                # case state. Asking the model to predict it costs a wasted
                # regeneration every time it guesses the other way, so it always
                # places the marker and the runtime decides whether text lands.
                lines.append(
                    f"- Always emit {block.marker} in a completed draft. Whether"
                    " its text applies is resolved by the runtime from the case"
                    " itself, and the marker is removed when it does not apply."
                )
            else:
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
    block_selection: Any = None,
    record_version_stamp: str | None = None,
    field_values: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Replace required markers with approved text or fail closed.

    ``block_selection`` carries a DETERMINISTIC, config-driven choice between
    approved blocks — ``substitutions`` maps an anchor block id to the variant
    that this case's product category actually requires, and ``forbid`` names
    blocks the category must never carry.  The model still decides WHERE the
    anchor marker goes; it never decides WHICH approved text lands there.
    """
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
    # Mutually exclusive underwriting scope (e.g. GIO vs UNDERWRITTEN): a
    # completed draft must declare EXACTLY ONE, and which one is a deterministic
    # property of the case's product category — never a model judgment. When the
    # category cannot be resolved, assembly FAILS CLOSED: an under-declared draft
    # silently omits a compliance sentence, so it must not ship.
    exclusive_scope_tags = {
        str(tag).strip().upper()
        for tag in (getattr(block_selection, "exclusive_scope_tags", None) or ())
        if str(tag).strip()
    }
    if exclusive_scope_tags and "DRAFT" in tags:
        selected_scope = getattr(block_selection, "scope_tag", None)
        selected_scope = str(selected_scope).strip().upper() if selected_scope else None
        if selected_scope not in exclusive_scope_tags:
            raise PAOutputAssemblyError(
                "a completed draft must declare exactly one of "
                + "/".join(sorted(exclusive_scope_tags))
                + "; the case record's product category resolved to "
                + repr(getattr(block_selection, "category", None))
                + ", which maps to no scope"
            )
        tags = frozenset((tags - exclusive_scope_tags) | {selected_scope})

    # Additive runtime scope tags: category facts the case record already knows
    # (this case IS an ILP) must not be re-predicted by the model. Unlike the
    # exclusive set these do not displace anything — they union in, so a block
    # gated on one becomes required whether or not the model declared it.
    additional_scope_tags = {
        str(tag).strip().upper()
        for tag in (getattr(block_selection, "additional_scope_tags", None) or ())
        if str(tag).strip()
    }
    if additional_scope_tags and "DRAFT" in tags:
        tags = frozenset(tags | additional_scope_tags)

    substitutions = {
        str(key): str(value)
        for key, value in (getattr(block_selection, "substitutions", None) or {}).items()
    }
    forbidden = {
        str(item) for item in (getattr(block_selection, "forbidden", None) or ())
    }
    by_id = {block.block_id: block for block in blocks}
    unknown = [
        block_id
        for block_id in list(substitutions) + list(substitutions.values()) + list(forbidden)
        if block_id not in by_id
    ]
    if unknown:
        raise PAOutputAssemblyError(
            "disclaimer selection names unknown block ids: " + ", ".join(sorted(unknown))
        )
    required = [
        block
        for block in blocks
        if block.required_tags.issubset(tags)
        and block.block_id not in forbidden
        and not block.selection_only
    ]
    # A substitution variant is selected BY CATEGORY, never by a model tag: it
    # is inserted at its anchor's marker, so it is not required on its own.
    variant_ids = set(substitutions.values())
    required = [block for block in required if block.block_id not in variant_ids]
    # A slotted block whose case-record values do not resolve is DROPPED rather
    # than inserted half-filled: a sentence with an unfilled hole is a
    # client-visible error, and a regeneration cannot conjure a fact the record
    # does not hold. The omission is recorded, never silent.
    rendered_text: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []
    for block in list(required):
        selected = by_id.get(substitutions.get(block.block_id, ""), block)
        text = selected.render(field_values)
        if text is None:
            required.remove(block)
            unresolved.append(
                {
                    "id": selected.block_id,
                    "artifact": selected.artifact,
                    "unresolved_fields": [slot.field for slot in selected.slots],
                }
            )
            continue
        rendered_text[block.block_id] = text
    # DROP IS NOT A LICENCE TO PARAPHRASE.  Dropping the approved sentence
    # removes the RUNTIME's text; it does nothing about the model's own
    # sentence on the same subject, which is exactly what the marker
    # discipline exists to keep out of the draft.  So when a dropped block
    # declares how its topic reads in prose and the model's own text (checked
    # BEFORE any approved text is spliced in, so every match is model-authored)
    # speaks to it, assembly fails closed rather than shipping the paraphrase.
    guard_subject = _MARKER_RE.sub(" ", assembled) if unresolved else assembled
    for entry in unresolved:
        guarded = by_id.get(str(entry.get("id") or ""))
        if guarded is None or not guarded.drop_guard:
            continue
        hits = [
            pattern.pattern
            for pattern in guarded.drop_guard
            if pattern.search(guard_subject)
        ]
        if not hits:
            continue
        entry["model_authored_topic"] = hits
        raise PAOutputAssemblyRetry(
            (guarded.marker,),
            (
                f"block {guarded.block_id} was dropped because the case record does "
                f"not resolve {', '.join(slot.field for slot in guarded.slots)}, but "
                "the draft states its subject in the model's own words"
            ),
            instruction=(
                "OUTPUT COMPLIANCE RETRY: the previous draft was withheld by the "
                "runtime. It stated in your own words something only an approved "
                "block may state, and the case record does not hold the facts that "
                "block needs. Regenerate the entire answer: do not write that "
                "sentence yourself and do not restate its content anywhere. If the "
                "case genuinely lacks those facts, ask for them instead of drafting."
            ),
        )
    missing = [
        block.marker
        for block in required
        if assembled.count(block.marker) < 1
        or (not block.repeatable and assembled.count(block.marker) != 1)
    ]
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
            selected = by_id.get(substitutions.get(block.block_id, ""), block)
            text = rendered_text[block.block_id]
            assembled = assembled.replace(
                block.marker, text, count if block.repeatable else 1
            )
            inserted.append(
                {
                    "id": selected.block_id,
                    "artifact": selected.artifact,
                    "approved_by": selected.provenance.get("approved_by"),
                    "approved_date": selected.provenance.get("approved_date"),
                    "ruling_ref": selected.provenance.get("ruling_ref"),
                    "status": selected.provenance.get("status"),
                    **({"occurrences": count} if block.repeatable else {}),
                    **(
                        {"substituted_for": block.block_id}
                        if selected is not block
                        else {}
                    ),
                }
            )
        elif count:
            assembled = assembled.replace(block.marker, "")
    if "[[PA_BLOCK:" in assembled or "[[PA_SCOPE:" in assembled:
        raise PAOutputAssemblyRetry(("no residual PA markers",), "unknown marker remains")
    evidence: dict[str, Any] = {
        "enabled": True,
        "scope_tags": sorted(tags),
        "deterministic_scope": deterministic_scope,
        "inserted": inserted,
    }
    if unresolved:
        evidence["unresolved_slots"] = unresolved
    if block_selection is not None:
        to_dict = getattr(block_selection, "to_dict", None)
        evidence["disclaimer_selection"] = (
            to_dict() if callable(to_dict) else dict(block_selection or {})
        )
    if record_version_stamp:
        evidence["record_version_stamp"] = record_version_stamp
    return assembled.strip(), evidence


def retry_instruction(error: PAOutputAssemblyRetry) -> str:
    if error.instruction:
        return error.instruction
    markers = ", ".join(error.missing_markers)
    return (
        "OUTPUT COMPLIANCE RETRY: the previous draft was withheld by the runtime. "
        f"Regenerate the entire answer and include exactly one scope marker plus: {markers}. "
        "Do not type the protected compliance wording yourself."
    )
