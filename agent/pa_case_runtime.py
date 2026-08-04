"""Runtime wiring for the PA structured case record.

``agent/pa_case_record.py`` owns the SHAPE (what a case is, what a recorded
field carries, which required fields are still empty).  This module is the
RUNTIME side of the same substrate: it decides, once per inbound turn, which
case the turn belongs to, records what the turn supplied, derives what can be
derived, and renders the record back into the turn as the authoritative case
state the model must work from.

WHAT IT REPLACES
----------------
Without this layer the agent re-reads the conversation every turn: it asks
again for facts it was already given, and a draft has no stable thing it was
built FROM.  With it:

* **Mint on the runtime's own new-case boundary.**  A fresh scope, a reset
  session (``/new`` starts a new session id), or a message the boundary
  classifier calls a NEW case mints a record.  Nothing depends on a user
  typing a command.
* **Facts are recorded with the message they came from.**  A small extraction
  pass reads ONE message against the field-set contract and writes each stated
  value with ``source_message_id``.  Values that merely repeat what is already
  recorded are skipped, so the version only moves when the case moves.
* **Intake asks EXACTLY the empty required set.**  The rendered block names
  the still-missing askable fields and nothing else; a filled or derived field
  is never in that set, so it is never re-asked.
* **The draft is built FROM the record**, and carries the record's version
  stamp (``<case_id>@vN``) in runtime evidence.

UNIVERSALITY
------------
Every field id, case type, ask hint, derivation rule, and disclaimer mapping
is CLIENT CONFIG read from the deployment tree.  This module never inspects
what a field id means: it moves opaque strings between config, the record, the
prompt, and the assembly layer.  No client vocabulary belongs here.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.pa_case_record import (
    CANON_UNMAPPABLE,
    CaseRecord,
    CaseRecordStore,
    FieldSet,
    FieldSpec,
    ValueContract,
    ValueTable,
    empty_required_fields,
    load_field_sets,
    parse_value_table_entries,
    parse_value_tables,
    select_field_set,
    value_contracts_from_field_sets,
)

logger = logging.getLogger(__name__)


BOUNDARY_CONTINUE = "continue"
BOUNDARY_NEW = "new"
BOUNDARY_AMBIGUOUS = "ambiguous"
_BOUNDARIES = (BOUNDARY_CONTINUE, BOUNDARY_NEW, BOUNDARY_AMBIGUOUS)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


# ── policy plumbing ─────────────────────────────────────────────────────


def case_record_policy(pa_context: Any) -> Mapping[str, Any]:
    brief = getattr(pa_context, "job_brief", None)
    response_policy = getattr(brief, "response_policy", None)
    if not isinstance(response_policy, Mapping):
        return {}
    policy = response_policy.get("case_record")
    return policy if isinstance(policy, Mapping) else {}


def case_record_enabled(pa_context: Any) -> bool:
    return bool(case_record_policy(pa_context).get("enabled", False))


def _knowledge_root(knowledge_root: str | Path | None) -> Path:
    if knowledge_root is not None:
        return Path(knowledge_root).resolve()
    from hermes_constants import get_hermes_home

    return (get_hermes_home() / "knowledge").resolve()


def _resolve_config_path(relative: str, knowledge_root: str | Path | None) -> Path:
    root = _knowledge_root(knowledge_root)
    candidate = Path(str(relative))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"case-record config path must be relative: {relative}")
    path = (root / candidate).resolve()
    path.relative_to(root)  # raises if the entry escapes the knowledge root
    return path


# ── client config ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class DerivationRule:
    """One config-declared derivation: target <- source, by matching rules."""

    target_field_id: str
    source_field_id: str
    #: (pattern, fixed value, template).  A template expands the match's own
    #: groups (``\1``), so a rule can lift a value OUT of the source answer
    #: instead of only mapping it to a constant.
    matches: Tuple[Tuple[str, Any, Optional[str]], ...] = ()
    default: Any = None

    def resolve(self, source_value: Any) -> Any:
        text = "" if source_value is None else str(source_value)
        for pattern, value, template in self.matches:
            try:
                found = re.search(pattern, text)
            except re.error:
                continue
            if not found:
                continue
            if template:
                try:
                    return found.expand(template)
                except (re.error, IndexError):
                    continue
            return value
        return self.default


@dataclass(frozen=True)
class CaseRuntimeConfig:
    field_sets: Mapping[str, FieldSet]
    #: Reference tables a table-kind value contract checks against, already
    #: resolved out of the deployment tree.
    value_tables: Mapping[str, ValueTable] = dc_field(default_factory=dict)
    derivations: Tuple[DerivationRule, ...] = ()
    default_case_type: Optional[str] = None
    category_field_id: Optional[str] = None
    category_source_field_id: Optional[str] = None
    extraction_enabled: bool = True
    extraction_max_tokens: int = 900
    extraction_timeout: float = 45.0
    extraction_task: str = "pa_case_extraction"
    extraction_model: Optional[str] = None
    extraction_provider: Optional[str] = None

    @property
    def case_types(self) -> List[str]:
        return [fs.case_type or fs.field_set_id for fs in self.field_sets.values()]

    @property
    def value_contracts(self) -> Mapping[str, ValueContract]:
        """Every contracted field the deployment declares, across case types.

        Handed to the store so that EVERY write is checked — the record write
        is the single place a value contract is enforced.
        """
        return value_contracts_from_field_sets(self.field_sets)

    def open_store(self, session_db: Any) -> CaseRecordStore:
        return CaseRecordStore(
            session_db,
            value_contracts=self.value_contracts,
            value_tables=self.value_tables,
        )


def _parse_derivations(raw: Any) -> Tuple[DerivationRule, ...]:
    """Parse the client-owned derivation table.

    Shape (values are entirely client vocabulary)::

        derivations:
          <target field id>:
            from: <source field id>
            matches:
              - pattern: "(?i)^\\s*(none|no existing)"
                value: "no"
            default: "yes"

    A target may also declare a LIST of rules, tried in order against whichever
    source answer is recorded — the first rule that resolves wins, so a target
    can be reachable from more than one answer::

        derivations:
          <target field id>:
            - from: <source a>
              matches: [...]
            - from: <source b>
              matches: [...]
    """
    if not isinstance(raw, Mapping):
        return ()
    rules: List[DerivationRule] = []
    for target, spec in raw.items():
        entries = spec if isinstance(spec, (list, tuple)) else [spec]
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            source = entry.get("from") or entry.get("derived_from")
            if not source:
                continue
            matches: List[Tuple[str, Any, Optional[str]]] = []
            for item in entry.get("matches") or ():
                if not isinstance(item, Mapping):
                    continue
                pattern = item.get("pattern")
                if not pattern:
                    continue
                template = item.get("value_template")
                matches.append(
                    (
                        str(pattern),
                        item.get("value"),
                        str(template) if template else None,
                    )
                )
            rules.append(
                DerivationRule(
                    target_field_id=str(target),
                    source_field_id=str(source),
                    matches=tuple(matches),
                    default=entry.get("default"),
                )
            )
    return tuple(rules)


def load_value_tables(
    raw: Any,
    *,
    knowledge_root: str | Path | None = None,
) -> Dict[str, ValueTable]:
    """Resolve the deployment's value tables, following ``source:`` files.

    A table either declares its entries inline or points at a REFERENCE FILE
    that some other owner maintains on its own schedule (the approved-product
    list, the fund list).  Pointing is the better shape: copying those values
    into the field-set config would fork them the first time the real file
    changed, and the copy would keep answering with confidence.

    The file's own column names are read from the table's declaration
    (``select`` / ``value_key`` / ``aliases_key``), so the reference file is
    never reshaped to suit this consumer.

    A table whose source cannot be read is DROPPED with a warning rather than
    failing the deployment: the contract that names it then accepts answers as
    written (see ``ValueContract._resolve_table``).  A missing table must not
    silently become a refusal of every answer.
    """
    tables = parse_value_tables({"value_tables": raw} if raw else {})
    if not tables:
        return {}
    import yaml

    resolved: Dict[str, ValueTable] = {}
    raw_tables = raw if isinstance(raw, Mapping) else {}
    for table_id, table in tables.items():
        declaration = raw_tables.get(table_id) or {}
        declaration = declaration if isinstance(declaration, Mapping) else {}
        source = table.source
        if not source:
            resolved[table_id] = table
            continue
        try:
            path = _resolve_config_path(str(source), knowledge_root)
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, Mapping):
                raise ValueError(f"value table source must be a mapping: {path}")
            select = str(declaration.get("select") or "entries")
            rows = data.get(select)
            if not isinstance(rows, (list, tuple)):
                raise ValueError(
                    f"value table source {source} has no list at {select!r}"
                )
            entries = parse_value_table_entries(
                rows,
                table_id=table_id,
                value_key=str(declaration.get("value_key") or "key"),
                aliases_key=str(declaration.get("aliases_key") or "aliases"),
            )
            resolved[table_id] = ValueTable(
                table_id=table_id,
                entries=tuple(table.entries) + entries,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 — a bad table never blocks intake
            logger.warning(
                "value table %r could not be loaded from %r (%s); contracts "
                "naming it will accept answers as written",
                table_id,
                source,
                exc,
            )
    return resolved


def load_case_runtime_config(
    pa_context: Any,
    *,
    knowledge_root: str | Path | None = None,
) -> CaseRuntimeConfig:
    policy = case_record_policy(pa_context)
    field_sets_entry = policy.get("field_sets")
    if not field_sets_entry:
        raise ValueError("case_record.field_sets must name a knowledge entry")
    path = _resolve_config_path(str(field_sets_entry), knowledge_root)
    field_sets = load_field_sets(path)

    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    derivations = _parse_derivations(raw.get("derivations") if isinstance(raw, Mapping) else None)
    value_tables = load_value_tables(
        raw.get("value_tables") if isinstance(raw, Mapping) else None,
        knowledge_root=knowledge_root,
    )

    extraction = policy.get("extraction")
    extraction = extraction if isinstance(extraction, Mapping) else {}
    return CaseRuntimeConfig(
        field_sets=field_sets,
        value_tables=value_tables,
        derivations=derivations,
        default_case_type=(
            str(policy["default_case_type"]) if policy.get("default_case_type") else None
        ),
        category_field_id=(
            str(policy["category_field"]) if policy.get("category_field") else None
        ),
        category_source_field_id=(
            str(policy["category_source_field"])
            if policy.get("category_source_field")
            else None
        ),
        extraction_enabled=bool(extraction.get("enabled", True)),
        extraction_max_tokens=int(extraction.get("max_tokens", 900) or 900),
        extraction_timeout=float(extraction.get("timeout", 45.0) or 45.0),
        extraction_task=str(extraction.get("task", "pa_case_extraction")),
        extraction_model=(str(extraction["model"]) if extraction.get("model") else None),
        extraction_provider=(
            str(extraction["provider"]) if extraction.get("provider") else None
        ),
    )


# ── turn state ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaseTurnState:
    """What the case record says about THIS turn."""

    record: CaseRecord
    field_set: Optional[FieldSet]
    empty_fields: Tuple[FieldSpec, ...] = ()
    minted: bool = False
    boundary: str = BOUNDARY_CONTINUE
    category: Optional[str] = None
    recorded_field_ids: Tuple[str, ...] = ()
    derived_field_ids: Tuple[str, ...] = ()
    extraction_ok: bool = True
    #: Contracted fields whose answer resolved to no permitted value.  They
    #: are EMPTY (so they are in ``empty_fields`` and intake asks), but the
    #: ask is a clarification of something already said.
    unmappable_field_ids: Tuple[str, ...] = ()
    #: Carried so the rendered intake block can name the permitted values in
    #: the clarification question — an ask that repeats the original question
    #: verbatim invites the same unusable answer a second time.
    value_contracts: Mapping[str, ValueContract] = dc_field(default_factory=dict)
    value_tables: Mapping[str, ValueTable] = dc_field(default_factory=dict)

    @property
    def version_stamp(self) -> str:
        return self.record.version_stamp()

    def evidence(self) -> Dict[str, Any]:
        return {
            "case_id": self.record.case_id,
            "record_version": self.record.record_version,
            "version_stamp": self.version_stamp,
            "case_type": self.record.case_type,
            "category": self.category,
            "minted": self.minted,
            "boundary": self.boundary,
            "recorded_fields": list(self.recorded_field_ids),
            "derived_fields": list(self.derived_field_ids),
            "empty_required_fields": [spec.field_id for spec in self.empty_fields],
            "unmappable_fields": list(self.unmappable_field_ids),
            "extraction_ok": self.extraction_ok,
        }


# ── extraction ──────────────────────────────────────────────────────────


_EXTRACTION_SYSTEM = (
    "You extract structured case facts from ONE message in an ongoing "
    "conversation, for a runtime-maintained case record. You never answer the "
    "message and never invent values. Reply with JSON only."
)


def build_extraction_prompt(
    *,
    message: str,
    message_id: Optional[str],
    field_set: Optional[FieldSet],
    record: Optional[CaseRecord],
    case_types: Sequence[str],
    has_open_case: bool,
) -> str:
    lines: List[str] = []
    lines.append("FIELDS THE RECORD HOLDS (id — what it holds):")
    for spec in (field_set.fields if field_set else ()):
        # `holds` describes the value; `ask_hint` is the question and only
        # stands in when the deployment has not written a description.
        hint = (spec.holds or spec.ask_hint or spec.notes or "").strip().replace("\n", " ")
        lines.append(f"- {spec.field_id}" + (f" — {hint}" if hint else ""))
    if case_types:
        lines.append("")
        lines.append("CASE TYPES: " + ", ".join(case_types))
    if record is not None and record.fields:
        lines.append("")
        lines.append("ALREADY RECORDED (values from earlier messages of this case):")
        for field_id, entry in record.fields.items():
            if entry.is_filled:
                lines.append(f"- {field_id} = {json.dumps(entry.value, default=str)}")
    lines.append("")
    lines.append(f"NEW MESSAGE (id {message_id or 'unknown'}):")
    lines.append(str(message))
    lines.append("")
    lines.append("Return ONLY this JSON object:")
    lines.append(
        '{"boundary": "continue" | "new" | "ambiguous", '
        '"case_type": "<one of the case types above, or null>", '
        '"fields": {"<field id>": <value or null>}}'
    )
    lines.append("")
    lines.append("Rules:")
    lines.append(
        "- fields: include ONLY ids listed above whose value this message states "
        "or clearly implies. Copy the writer's own wording; never guess, never "
        "restate a recorded value that did not change."
    )
    lines.append(
        "- A value that CONTRADICTS a recorded value is a correction: include it "
        "with the new value."
    )
    # A field description says what the field will EVENTUALLY hold, so it reads
    # like a checklist the message has to satisfy: an extractor shown a full
    # description withholds a short answer as "not stated", and the record loses
    # a fact the writer plainly gave — most often on the case-opening message,
    # which names the subject and nothing else. What is still missing is
    # computed from the record afterwards; it is not the extractor's call.
    lines.append(
        "- Record what the message DOES state even when it is less complete than "
        "the field description: the description is what the field will hold in "
        "the end, not a bar the message must clear. A short or informal name for "
        "something IS a stated value, so record the name the writer used, "
        "verbatim and on its own. A later message with fuller detail replaces it."
    )
    if has_open_case:
        lines.append(
            '- boundary "new" ONLY when this message opens a DIFFERENT case '
            "(different subject/client, a different set of items, or the writer "
            'says it is a new one). Adding or correcting a detail is "continue". '
            'Use "ambiguous" when it could genuinely be either.'
        )
    else:
        lines.append('- boundary: always "new" — no case is open yet.')
    lines.append("- case_type: your best classification of THIS case, else null.")
    return "\n".join(lines)


def parse_extraction_response(text: str) -> Dict[str, Any]:
    """Parse the extractor's JSON, tolerating fences and stray prose."""
    raw = (text or "").strip()
    if not raw:
        return {}
    fenced = _JSON_FENCE_RE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    boundary = str(data.get("boundary") or BOUNDARY_CONTINUE).strip().lower()
    if boundary not in _BOUNDARIES:
        boundary = BOUNDARY_CONTINUE
    fields = data.get("fields")
    fields = dict(fields) if isinstance(fields, Mapping) else {}
    case_type = data.get("case_type")
    return {
        "boundary": boundary,
        "case_type": str(case_type) if case_type else None,
        "fields": {str(k): v for k, v in fields.items() if v is not None},
    }


async def _run_extraction(
    *,
    config: CaseRuntimeConfig,
    prompt: str,
) -> Dict[str, Any]:
    from agent.auxiliary_client import async_call_llm

    response = await async_call_llm(
        task=config.extraction_task,
        provider=config.extraction_provider,
        model=config.extraction_model,
        messages=[
            {"role": "system", "content": _EXTRACTION_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=config.extraction_max_tokens,
        timeout=config.extraction_timeout,
    )
    content = ""
    try:
        content = response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        content = ""
    return parse_extraction_response(content)


# ── the per-turn entry point ────────────────────────────────────────────


def _category_of(
    record: CaseRecord,
    config: CaseRuntimeConfig,
    fallback: Optional[str] = None,
) -> Optional[str]:
    if config.category_field_id:
        value = record.value_of(config.category_field_id)
        if value:
            return str(value)
    return record.case_type or fallback or config.default_case_type


def _apply_config_derivations(
    store: CaseRecordStore,
    case_id: str,
    config: CaseRuntimeConfig,
) -> List[str]:
    if not config.derivations:
        return []
    by_target: Dict[str, List[DerivationRule]] = {}
    for rule in config.derivations:
        by_target.setdefault(rule.target_field_id, []).append(rule)

    def _make(rules: List[DerivationRule]):
        def _derive(record: CaseRecord):
            # First rule whose source answer is recorded AND resolves wins.
            for rule in rules:
                source = record.get(rule.source_field_id)
                if source is None or not source.is_filled:
                    continue
                resolved = rule.resolve(source.value)
                if resolved is None:
                    continue
                return resolved, rule.source_field_id
            return None

        return _derive

    derivers = {target: _make(rules) for target, rules in by_target.items()}
    try:
        return list(store.apply_derivations(case_id, derivers))
    except Exception as exc:  # noqa: BLE001 — recording never breaks the turn
        logger.debug("PA case derivation failed (non-fatal): %s", exc)
        return []


def _extraction_field_set(config: CaseRuntimeConfig) -> Optional[FieldSet]:
    """Every field id any contract declares, as one set for extraction.

    Reading a message is not the same job as deciding what is still missing:
    the second needs ONE contract, the first must not presuppose which.  Ids
    are opaque and shared across contracts by design, so the union is just the
    vocabulary the deployment can record; the first contract to declare an id
    supplies its description.
    """
    merged: Dict[str, FieldSpec] = {}
    for field_set in config.field_sets.values():
        for spec in field_set.fields:
            merged.setdefault(spec.field_id, spec)
    if not merged:
        return None
    return FieldSet(
        field_set_id="__all_contracts__",
        case_type=None,
        fields=tuple(merged.values()),
    )


async def update_case_for_turn(
    *,
    session_db: Any,
    pa_context: Any,
    agent_id: Optional[str],
    chat_id: Optional[str],
    session_id: Optional[str],
    message: str,
    message_id: Optional[str],
    knowledge_root: str | Path | None = None,
    config: Optional[CaseRuntimeConfig] = None,
) -> Optional[CaseTurnState]:
    """Resolve the turn's case, record what the turn supplied, return state.

    Returns ``None`` when the deployment has no case record configured.  Every
    failure inside is swallowed to a debug log: the case record is substrate
    for the turn, never a gate on it.
    """
    if session_db is None or not case_record_enabled(pa_context):
        return None
    if config is None:
        config = load_case_runtime_config(pa_context, knowledge_root=knowledge_root)

    # The store carries the deployment's enum contracts, so every write this
    # turn makes — stated, category-derived, or config-derived — canonicalises
    # at the record rather than leaving a variant for a downstream reader.
    store = config.open_store(session_db)
    record = store.get_open_case(agent_id=agent_id, chat_id=chat_id)

    # A reset session (/new) is a boundary the runtime already owns: the case
    # scope survives it, so an open case from the previous session must not
    # bleed into the fresh one.
    session_reset = bool(
        record is not None
        and session_id
        and record.session_id
        and record.session_id != session_id
    )

    live_category = _category_of(record, config) if record is not None else None
    field_set = select_field_set(
        config.field_sets,
        case_type=live_category or config.default_case_type,
    )

    extraction: Dict[str, Any] = {}
    extraction_ok = True
    if config.extraction_enabled:
        try:
            extraction = await _run_extraction(
                config=config,
                prompt=build_extraction_prompt(
                    message=message,
                    message_id=message_id,
                    # EXTRACT AGAINST EVERY CONTRACT, CLASSIFY AFTERWARDS. The
                    # contract a message belongs to is decided BY that message,
                    # so offering the extractor only the contract the case is
                    # currently on loses exactly the facts that reveal the real
                    # one: a case is minted on the default type, and the opening
                    # message's type-specific facts have no id to land in. They
                    # then read downstream as never stated — intake re-asks
                    # them, and an approved sentence whose slots live in those
                    # fields cannot resolve, so it is dropped from a draft the
                    # writer gave every fact for. Classification still picks ONE
                    # contract below, and the write loop keeps out anything that
                    # contract does not declare.
                    field_set=_extraction_field_set(config),
                    record=record,
                    case_types=config.case_types,
                    has_open_case=record is not None and not session_reset,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — extraction never breaks a turn
            logger.debug("PA case extraction failed (non-fatal): %s", exc)
            extraction_ok = False
            extraction = {}

    boundary = str(extraction.get("boundary") or BOUNDARY_CONTINUE)
    extracted_case_type = extraction.get("case_type") or None
    if extracted_case_type and extracted_case_type not in config.case_types:
        extracted_case_type = None

    minted = False
    if record is None or session_reset or boundary == BOUNDARY_NEW:
        case_type = extracted_case_type or config.default_case_type
        case_field_set = select_field_set(config.field_sets, case_type=case_type)
        case_id = store.mint_case(
            case_type=case_type,
            agent_id=agent_id,
            chat_id=chat_id,
            session_id=session_id,
            field_set_id=case_field_set.field_set_id if case_field_set else None,
            minted_from_message_id=message_id,
        )
        minted = True
        boundary = BOUNDARY_NEW if not session_reset else BOUNDARY_NEW
        record = store.get_case(case_id)
    if record is None:
        return None

    # Field set for the (possibly re-classified) case.
    category = extracted_case_type or _category_of(record, config)
    field_set = (
        select_field_set(config.field_sets, case_type=category)
        or select_field_set(config.field_sets, case_type=config.default_case_type)
    )

    recorded: List[str] = []
    known_ids = {spec.field_id for spec in (field_set.fields if field_set else ())}
    for field_id, value in (extraction.get("fields") or {}).items():
        if field_id not in known_ids:
            continue
        spec = field_set.spec(field_id) if field_set else None
        if spec is not None and not spec.askable and spec.derived_from:
            # Derived fields are the derivation layer's to write, never the
            # extractor's — that is what keeps their provenance honest.
            continue
        current = record.get(field_id)
        # Compare against the CONTRACTED form: an advisor restating
        # "aggressive" over a stored "Aggressive" has changed nothing about
        # the case, and bumping the record version on it would say otherwise.
        candidate, _outcome = store.canonicalize(field_id, value, record=record)
        if current is not None and current.is_filled and current.value == candidate:
            continue  # nothing changed: do not churn the version
        try:
            store.record_field(
                record.case_id,
                field_id,
                value,
                source_message_id=message_id,
            )
            recorded.append(field_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PA case field write failed (non-fatal): %s", exc)

    # The product category is DERIVED from the answer that revealed it, so it
    # carries that answer's provenance rather than the turn it was inferred on.
    refreshed = store.get_case(record.case_id) or record
    if (
        config.category_field_id
        and category
        and config.category_source_field_id
        and refreshed.is_filled(config.category_source_field_id)
        and refreshed.value_of(config.category_field_id) != category
    ):
        try:
            store.record_derived_field(
                refreshed.case_id,
                config.category_field_id,
                category,
                derived_from_field_id=config.category_source_field_id,
            )
            recorded.append(config.category_field_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("PA category derivation failed (non-fatal): %s", exc)

    derived = _apply_config_derivations(store, record.case_id, config)
    final = store.get_case(record.case_id) or record
    empty = tuple(empty_required_fields(final, field_set)) if field_set else ()
    unmappable = tuple(
        field_id
        for field_id, entry in final.fields.items()
        if entry.is_unmappable
    )
    return CaseTurnState(
        record=final,
        field_set=field_set,
        empty_fields=empty,
        minted=minted,
        boundary=boundary,
        category=_category_of(final, config, fallback=category),
        recorded_field_ids=tuple(recorded),
        derived_field_ids=tuple(derived),
        extraction_ok=extraction_ok,
        unmappable_field_ids=unmappable,
        value_contracts=config.value_contracts,
        value_tables=config.value_tables,
    )


# ── the prompt surface ──────────────────────────────────────────────────


def render_case_record_prompt(state: Optional[CaseTurnState]) -> str:
    """Render the record as the turn's authoritative case state.

    This is the intake surface and the draft's source of facts in one block:
    what is known (never ask again), what is still missing (ask EXACTLY these),
    and the version the draft is built from.
    """
    if state is None or state.record is None:
        return ""
    record = state.record
    lines = [
        "## CASE RECORD (runtime-maintained — authoritative for this case)",
        f"Case {record.version_stamp()}"
        + (f" · type: {state.category}" if state.category else "")
        + (" · opened by this message" if state.minted else ""),
    ]
    filled = [
        (field_id, entry)
        for field_id, entry in record.fields.items()
        if entry.is_filled
    ]
    if filled:
        lines.append("")
        lines.append(
            "FACTS ALREADY GIVEN — the advisor supplied these; NEVER ask for any "
            "of them again, and build the draft only from them:"
        )
        for field_id, entry in filled:
            value = entry.value
            rendered = value if isinstance(value, str) else json.dumps(value, default=str)
            suffix = (
                f" (derived from {entry.derived_from_field_id})"
                if entry.derived_from_field_id
                else ""
            )
            lines.append(f"- {field_id}: {rendered}{suffix}")
    else:
        lines.append("")
        lines.append("FACTS ALREADY GIVEN: none yet.")

    lines.append("")
    if state.empty_fields:
        lines.append(
            "STILL MISSING — ask for EXACTLY these, in one compact question, and "
            "nothing else:"
        )
        for spec in state.empty_fields:
            hint = (spec.ask_hint or "").strip().replace("\n", " ")
            entry = record.get(spec.field_id)
            if entry is not None and entry.is_unmappable:
                # ASK A CLARIFICATION, NOT THE ORIGINAL QUESTION AGAIN.  The
                # advisor DID answer; the answer just is not one this case
                # can be built on.  Repeating the question as if nothing was
                # said reads as not listening, and usually earns the same
                # unusable answer a second time.
                contract = state.value_contracts.get(spec.field_id)
                permitted = (
                    contract.describe_values(state.value_tables)
                    if contract is not None
                    else ""
                )
                said = (entry.raw_value or "").strip().replace("\n", " ")
                clarify = f"{hint or spec.field_id} — the advisor answered"
                if said:
                    clarify += f' "{said}"'
                clarify += ", which is not one this case can use"
                if permitted:
                    clarify += f"; it must be one of: {permitted}"
                clarify += (
                    ". Ask them to confirm which one it is; never pick one for "
                    "them."
                )
                lines.append(f"- {clarify}")
                continue
            lines.append(f"- {hint or spec.field_id}")
    else:
        lines.append(
            "STILL MISSING: nothing. Do not ask any intake question — draft now "
            "from the facts above."
        )
    if state.boundary == BOUNDARY_AMBIGUOUS:
        lines.append("")
        lines.append(
            "BOUNDARY UNCLEAR: this message may open a new case or add detail to "
            "this one. Ask ONE short question to settle it before drafting."
        )
    lines.append("")
    lines.append(
        f"Any draft you produce in this turn is built FROM this record at "
        f"{record.version_stamp()}."
    )
    lines.append(
        "THIS BLOCK IS INTERNAL. Never quote it, its headings, its field names, "
        "or any other internal classification vocabulary back to the person you "
        "are writing to, and never mirror its bullet layout in a reply — ask and "
        "write in their own words, about their case."
    )
    return "\n".join(lines)


# ── disclaimer selection (deterministic, category-driven) ───────────────


@dataclass(frozen=True)
class DisclaimerSelection:
    """Which approved blocks a product category requires, resolved at assembly.

    ``scope_tag`` / ``exclusive_scope_tags`` carry the second consumer of the
    same product-category config: a completed draft must declare EXACTLY ONE of
    the mutually exclusive underwriting scopes, and which one is a deterministic
    property of the product category, never a model judgment.  A draft that can
    resolve neither fails assembly closed rather than shipping a draft that
    silently omits a compliance sentence.
    """

    category: Optional[str] = None
    substitutions: Mapping[str, str] = dc_field(default_factory=dict)
    forbidden: Tuple[str, ...] = ()
    source: Optional[str] = None
    matched: bool = False
    scope_tag: Optional[str] = None
    exclusive_scope_tags: Tuple[str, ...] = ()
    #: Additive scope tags the runtime resolves from the case's own category.
    #: They union into the model's scope rather than displacing anything, so a
    #: block gated on a category fact stops depending on the model re-declaring
    #: what the record already knows.
    additional_scope_tags: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "substitutions": dict(self.substitutions),
            "forbidden": list(self.forbidden),
            "source": self.source,
            "matched": self.matched,
            "scope_tag": self.scope_tag,
            "exclusive_scope_tags": list(self.exclusive_scope_tags),
            "additional_scope_tags": list(self.additional_scope_tags),
        }


def load_disclaimer_selection_config(
    entry: str,
    *,
    knowledge_root: str | Path | None = None,
) -> Mapping[str, Any]:
    import yaml

    path = _resolve_config_path(entry, knowledge_root)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"disclaimer-selection config must be a mapping: {path}")
    return data


def resolve_disclaimer_selection(
    *,
    session_db: Any,
    pa_context: Any,
    agent_id: Optional[str],
    chat_id: Optional[str],
    knowledge_root: str | Path | None = None,
) -> Optional[DisclaimerSelection]:
    """Pick the required disclaimer set from the case record's product category.

    The mapping is deploy-tree config: a product category names which approved
    block a shared anchor resolves to, and which blocks it must never carry.
    Selection is DETERMINISTIC — it does not consult the model's scope tags.
    """
    policy = case_record_policy(pa_context)
    entry = policy.get("disclaimer_selection")
    if not entry or session_db is None:
        return None
    try:
        config = load_disclaimer_selection_config(str(entry), knowledge_root=knowledge_root)
    except Exception as exc:  # noqa: BLE001 — selection never breaks a reply
        logger.debug("PA disclaimer-selection config unavailable: %s", exc)
        return None

    category: Optional[str] = None
    try:
        record = CaseRecordStore(session_db).get_open_case(
            agent_id=agent_id, chat_id=chat_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("PA disclaimer-selection record read failed: %s", exc)
        record = None
    if record is not None:
        selection_field = config.get("selection_field")
        if selection_field:
            value = record.value_of(str(selection_field))
            if value:
                category = str(value)
        if category is None and record.case_type:
            category = str(record.case_type)
    if category is None and config.get("default_category"):
        category = str(config["default_category"])

    categories = config.get("categories")
    categories = categories if isinstance(categories, Mapping) else {}
    entry_config = categories.get(category) if category else None
    if not isinstance(entry_config, Mapping):
        fallback = config.get("default_category")
        entry_config = categories.get(fallback) if fallback else None
        matched = False
    else:
        matched = True
    exclusive_scope_tags = tuple(
        str(tag).strip().upper()
        for tag in config.get("exclusive_scope_tags") or ()
        if str(tag).strip()
    )
    if not isinstance(entry_config, Mapping):
        # Unresolvable category: carry the contract WITHOUT a scope tag so
        # assembly fails closed instead of shipping an under-declared draft.
        return DisclaimerSelection(
            category=category,
            source=str(entry),
            matched=False,
            exclusive_scope_tags=exclusive_scope_tags,
        )

    raw_subs = entry_config.get("substitutions")
    substitutions = (
        {str(k): str(v) for k, v in raw_subs.items()}
        if isinstance(raw_subs, Mapping)
        else {}
    )
    forbidden = tuple(str(item) for item in entry_config.get("forbid") or ())
    scope_tag = entry_config.get("scope_tag")
    additional_scope_tags = tuple(
        str(tag).strip().upper()
        for tag in entry_config.get("scope_tags") or ()
        if str(tag).strip()
    )
    return DisclaimerSelection(
        category=category,
        substitutions=substitutions,
        forbidden=forbidden,
        source=str(entry),
        matched=matched,
        scope_tag=str(scope_tag).strip().upper() if scope_tag else None,
        exclusive_scope_tags=exclusive_scope_tags,
        additional_scope_tags=additional_scope_tags,
    )


__all__ = [
    "BOUNDARY_AMBIGUOUS",
    "BOUNDARY_CONTINUE",
    "BOUNDARY_NEW",
    "CaseRuntimeConfig",
    "CaseTurnState",
    "DerivationRule",
    "DisclaimerSelection",
    "build_extraction_prompt",
    "load_value_tables",
    "case_record_enabled",
    "case_record_policy",
    "load_case_runtime_config",
    "load_disclaimer_selection_config",
    "parse_extraction_response",
    "render_case_record_prompt",
    "resolve_disclaimer_selection",
    "update_case_for_turn",
]
