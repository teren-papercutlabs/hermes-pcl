"""PA structured case record — the per-case field substrate.

A deployed PA agent works one CASE at a time: a bounded unit of work opened
by whoever is talking to it.  This module is the SINGLE SOURCE OF TRUTH for
the case-record shape — what a case is, what a recorded field carries, and
which required fields are still empty.

WHY IT EXISTS
-------------
Without a record, everything the agent knows about the case lives only in
the conversation, so the agent re-reads (and re-asks) its way to an answer
every turn, and a produced draft has no stable thing it was built FROM.
The record turns the conversation into state: each field carries its VALUE,
the MESSAGE it came from, WHEN it was recorded, and WHETHER it was stated or
derived.

DESIGN INVARIANTS (ruled, not incidental)
-----------------------------------------
* **Minted on the runtime's own new-case boundary.**  Nothing here depends
  on a user typing a slash command; the boundary detector calls
  ``mint_case`` and the record exists.
* **Per field: value + source message id + timestamp + origin.**  A field
  with no provenance is not recordable — the columns are the contract.
* **A filled or derived field is NEVER re-asked.**  ``empty_required_fields``
  is the one query that decides what is still missing, and a field with a
  recorded value (whatever its origin) is not in that set.
* **Corrections UPDATE the record.**  The value is overwritten in place, the
  record version bumps, and the replaced value is kept in field history.
* **There is NO finalize / lock state.**  ``status`` is 'open' or
  'superseded', and 'superseded' means only that a new case was minted on
  the boundary.  Handing a draft over ENDS the flow — there is no
  post-handover state machine, and no state a correction has to unlock.
  Field history, not a lock, is the audit trail.

UNIVERSALITY
------------
``case_type`` and every field id are OPAQUE STRINGS supplied by field-set
config that a client deployment owns (see ``load_field_sets``).  No client
vocabulary — no product names, no domain nouns, no per-industry field ids —
belongs in this module or in the schema it writes to.  The module consumes
ANY field set.

LAYERING
--------
Storage (SQL, transactions, supersede-on-mint, version bumps, history) lives
on ``SessionDB`` in ``hermes_state.py``, alongside the other ``pa_*`` tables.
This module owns the shape, the config loader, and the queries that callers
actually reason with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence


# ── Origins ─────────────────────────────────────────────────────────────

#: The user told us this value (directly or by clear implication in their
#: message).  ``source_message_id`` is the message it came from.
#:
#: The design ruling names this origin after the human role a particular
#: deployment gives its counterpart ("advisor-stated").  The shared layer
#: cannot carry a client's role noun, so the same axis is spelled with the
#: generic actor: ``user_stated`` IS the stated-by-the-human origin.
ORIGIN_USER_STATED = "user_stated"

#: The agent worked this value out from ANOTHER recorded field rather than
#: asking for it.  ``derived_from_field_id`` names that field, and
#: ``source_message_id`` is the message the SOURCE answer came from — the
#: derivation inherits the provenance of the answer it stands on.
ORIGIN_DERIVED = "derived"

VALID_ORIGINS = (ORIGIN_USER_STATED, ORIGIN_DERIVED)

STATUS_OPEN = "open"
#: Set ONLY when a new case is minted on the boundary.  Not a lock.
STATUS_SUPERSEDED = "superseded"


# ── Shape contract ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaseFieldValue:
    """One recorded field: the value plus the provenance that earns it."""

    field_id: str
    value: Any = None
    origin: str = ORIGIN_USER_STATED
    source_message_id: Optional[str] = None
    recorded_at: Optional[float] = None
    #: Version of the case record at which THIS value landed.
    record_version: int = 0
    #: Set when origin is ``derived``.
    derived_from_field_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CaseFieldValue":
        return cls(
            field_id=row["field_id"],
            value=row.get("value"),
            origin=row.get("origin") or ORIGIN_USER_STATED,
            source_message_id=row.get("source_message_id"),
            recorded_at=row.get("recorded_at"),
            record_version=int(row.get("record_version") or 0),
            derived_from_field_id=row.get("derived_from_field_id"),
        )

    @property
    def is_filled(self) -> bool:
        """A field counts as filled when it holds any non-null value.

        Note that an explicit empty answer (``[]``, ``""``, ``"none"``) IS a
        value: the user answered.  Only ``None`` — nothing recorded — is
        empty, which is what keeps an answered-as-none field from being
        asked again.
        """
        return self.value is not None


@dataclass(frozen=True)
class CaseRecord:
    """A case plus its current field values.  Read-only snapshot."""

    case_id: str
    case_type: Optional[str] = None
    field_set_id: Optional[str] = None
    agent_id: Optional[str] = None
    chat_id: Optional[str] = None
    session_id: Optional[str] = None
    status: str = STATUS_OPEN
    #: Increments by exactly one on every field write.  0 = minted, empty.
    #: This is the stamp a draft built FROM the record carries.
    record_version: int = 0
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    minted_from_message_id: Optional[str] = None
    superseded_at: Optional[float] = None
    superseded_by_case_id: Optional[str] = None
    fields: Dict[str, CaseFieldValue] = dc_field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CaseRecord":
        raw_fields = row.get("fields") or {}
        return cls(
            case_id=row["case_id"],
            case_type=row.get("case_type"),
            field_set_id=row.get("field_set_id"),
            agent_id=row.get("agent_id"),
            chat_id=row.get("chat_id"),
            session_id=row.get("session_id"),
            status=row.get("status") or STATUS_OPEN,
            record_version=int(row.get("record_version") or 0),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            minted_from_message_id=row.get("minted_from_message_id"),
            superseded_at=row.get("superseded_at"),
            superseded_by_case_id=row.get("superseded_by_case_id"),
            fields={
                fid: CaseFieldValue.from_row(frow)
                for fid, frow in raw_fields.items()
            },
        )

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    def get(self, field_id: str) -> Optional[CaseFieldValue]:
        return self.fields.get(field_id)

    def value_of(self, field_id: str, default: Any = None) -> Any:
        entry = self.fields.get(field_id)
        return default if entry is None or not entry.is_filled else entry.value

    def is_filled(self, field_id: str) -> bool:
        entry = self.fields.get(field_id)
        return entry is not None and entry.is_filled

    def version_stamp(self) -> str:
        """The stamp a produced draft carries so it names what it was built FROM.

        Format: ``<case_id>@v<record_version>``.  A later reader can compare
        this against the live record and see, exactly, whether the draft
        predates a correction.
        """
        return f"{self.case_id}@v{self.record_version}"


# ── Field sets (client-owned config) ────────────────────────────────────


@dataclass(frozen=True)
class FieldSpec:
    """One field a case type may require.

    ``askable`` is the axis that keeps the intake honest: a field the agent
    is supposed to DERIVE or fill from a standard default is still part of
    the record, but asking for it is a defect — so it never appears in the
    empty-required set even when empty.
    """

    field_id: str
    required: bool = True
    askable: bool = True
    ask_hint: Optional[str] = None
    #: What the field HOLDS, for the reader that has to recognise a value in
    #: a message.  Distinct from ``ask_hint`` on purpose: an ask hint is a
    #: QUESTION and usually enumerates everything a complete answer would
    #: eventually carry, so reading it as a description turns it into a bar
    #: the message must clear — and a short but genuine answer gets dropped
    #: as "not stated".  Falls back to ``ask_hint`` when a deployment has not
    #: distinguished the two.
    holds: Optional[str] = None
    #: Field ids this one may be derived from.  Advisory metadata for the
    #: derivation layer; this module does not derive anything by itself.
    derived_from: Sequence[str] = ()
    #: Free-form, config-owned. Carries client-specific conditions the
    #: deployment interprets (e.g. "only when X"). Opaque here by design.
    applies_when: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class FieldSet:
    """The required-field contract for one case type."""

    field_set_id: str
    case_type: Optional[str] = None
    fields: Sequence[FieldSpec] = ()

    def spec(self, field_id: str) -> Optional[FieldSpec]:
        for spec in self.fields:
            if spec.field_id == field_id:
                return spec
        return None

    @property
    def required_field_ids(self) -> List[str]:
        return [f.field_id for f in self.fields if f.required]


def _spec_from_mapping(raw: Mapping[str, Any]) -> FieldSpec:
    field_id = raw.get("id") or raw.get("field_id")
    if not field_id:
        raise ValueError(f"field spec missing 'id': {raw!r}")
    return FieldSpec(
        field_id=str(field_id),
        required=bool(raw.get("required", True)),
        askable=bool(raw.get("askable", True)),
        ask_hint=raw.get("ask_hint"),
        holds=raw.get("holds"),
        derived_from=tuple(raw.get("derived_from") or ()),
        applies_when=raw.get("applies_when"),
        notes=raw.get("notes"),
    )


def parse_field_sets(data: Mapping[str, Any]) -> Dict[str, FieldSet]:
    """Build field sets from already-loaded config.

    Expected shape::

        version: 1
        field_sets:
          <field_set_id>:
            case_type: <optional, defaults to the id>
            fields:
              - id: <field id>
                required: true
                askable: true
                ask_hint: "..."     # the QUESTION intake composes from
                holds: "..."        # what the field HOLDS, for recognition
                derived_from: [<field id>, ...]

    Keys are opaque: this parser never inspects a field id's meaning.
    """
    raw_sets = data.get("field_sets") or {}
    if not isinstance(raw_sets, Mapping):
        raise ValueError("'field_sets' must be a mapping of id -> definition")

    out: Dict[str, FieldSet] = {}
    for set_id, raw in raw_sets.items():
        raw = raw or {}
        specs = [_spec_from_mapping(f) for f in (raw.get("fields") or [])]
        seen: set = set()
        for spec in specs:
            if spec.field_id in seen:
                raise ValueError(
                    f"field set '{set_id}' declares '{spec.field_id}' twice"
                )
            seen.add(spec.field_id)
        out[str(set_id)] = FieldSet(
            field_set_id=str(set_id),
            case_type=raw.get("case_type") or str(set_id),
            fields=tuple(specs),
        )
    return out


def load_field_sets(path: "str | Path") -> Dict[str, FieldSet]:
    """Load field sets from a YAML or JSON file.

    The file is CLIENT-OWNED config living in the deployment tree, not in
    this repo's shared layer — that is the whole point of it being a file.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".json",):
        data = json.loads(text)
    else:
        import yaml  # local import: keeps JSON-only callers yaml-free

        data = yaml.safe_load(text) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"field-set config must be a mapping: {p}")
    return parse_field_sets(data)


def select_field_set(
    field_sets: Mapping[str, FieldSet],
    *,
    case_type: Optional[str] = None,
    field_set_id: Optional[str] = None,
) -> Optional[FieldSet]:
    """Resolve a field set by explicit id, else by case type."""
    if field_set_id and field_set_id in field_sets:
        return field_sets[field_set_id]
    if case_type:
        if case_type in field_sets:
            return field_sets[case_type]
        for fs in field_sets.values():
            if fs.case_type == case_type:
                return fs
    return None


# ── The query phase-2 intake generation consumes ────────────────────────


def empty_required_fields(
    record: CaseRecord,
    field_set: FieldSet,
    *,
    include_unaskable: bool = False,
) -> List[FieldSpec]:
    """Required fields with nothing recorded yet — what may still be asked.

    THIS IS THE INTAKE SURFACE.  Question generation asks for exactly these
    and nothing else; an empty list means the record is complete enough to
    build from.

    A field is excluded when it is not required, when it already holds a
    value (whatever the origin — a DERIVED field is filled, so it is never
    re-asked), or when it is marked ``askable: false`` (the config says the
    agent must resolve it some other way, so asking is a defect).  Pass
    ``include_unaskable=True`` for the completeness view — everything still
    missing, regardless of who is supposed to supply it.

    Returns FieldSpecs in field-set declaration order, so the caller can ask
    in the order the config intends.
    """
    out: List[FieldSpec] = []
    for spec in field_set.fields:
        if not spec.required:
            continue
        if record.is_filled(spec.field_id):
            continue
        if not spec.askable and not include_unaskable:
            continue
        out.append(spec)
    return out


def empty_required_field_ids(
    record: CaseRecord,
    field_set: FieldSet,
    *,
    include_unaskable: bool = False,
) -> List[str]:
    """``empty_required_fields`` reduced to ids."""
    return [
        s.field_id
        for s in empty_required_fields(
            record, field_set, include_unaskable=include_unaskable
        )
    ]


def is_record_complete(record: CaseRecord, field_set: FieldSet) -> bool:
    """True when no required field is empty, askable or not."""
    return not empty_required_fields(record, field_set, include_unaskable=True)


# ── Store ───────────────────────────────────────────────────────────────


class CaseRecordStore:
    """Typed access to the case tables on a ``SessionDB``.

    Thin by design: the transactions live on SessionDB (alongside the other
    ``pa_*`` writers), and this class supplies the dataclass shapes plus the
    derivation hook.
    """

    def __init__(self, session_db: Any):
        self._db = session_db

    # ── mint ──

    def mint_case(
        self,
        *,
        case_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        session_id: Optional[str] = None,
        field_set_id: Optional[str] = None,
        minted_from_message_id: Optional[str] = None,
    ) -> str:
        """Mint a case on the new-case boundary; supersede the scope's open one.

        Called by the runtime's boundary detection, never by a user command.
        ``minted_from_message_id`` is the message the boundary fired on, so
        the record can always say which message opened it.
        """
        return self._db.mint_pa_case(
            case_type=case_type,
            agent_id=agent_id,
            chat_id=chat_id,
            session_id=session_id,
            field_set_id=field_set_id,
            minted_from_message_id=minted_from_message_id,
        )

    # ── read ──

    def get_case(self, case_id: str) -> Optional[CaseRecord]:
        row = self._db.get_pa_case(case_id)
        return CaseRecord.from_row(row) if row else None

    def get_open_case(
        self,
        *,
        agent_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Optional[CaseRecord]:
        row = self._db.get_open_pa_case(agent_id=agent_id, chat_id=chat_id)
        return CaseRecord.from_row(row) if row else None

    def field_history(
        self,
        case_id: str,
        field_id: Optional[str] = None,
    ) -> List[CaseFieldValue]:
        """Superseded values for a field (or the whole case), oldest first."""
        rows = self._db.list_pa_case_field_history(
            case_id=case_id, field_id=field_id
        )
        return [CaseFieldValue.from_row(r) for r in rows]

    # ── write ──

    def record_field(
        self,
        case_id: str,
        field_id: str,
        value: Any,
        *,
        source_message_id: Optional[str] = None,
        origin: str = ORIGIN_USER_STATED,
        derived_from_field_id: Optional[str] = None,
    ) -> int:
        """Record a stated value.  Returns the case's new record_version.

        A second call for the same field is a CORRECTION: it overwrites,
        bumps the version, and pushes the replaced value into history.
        """
        if origin not in VALID_ORIGINS:
            raise ValueError(
                f"origin must be one of {VALID_ORIGINS}, got {origin!r}"
            )
        if origin == ORIGIN_DERIVED and not derived_from_field_id:
            raise ValueError(
                "a derived field must name derived_from_field_id"
            )
        return self._db.record_pa_case_field(
            case_id=case_id,
            field_id=field_id,
            value=value,
            origin=origin,
            source_message_id=source_message_id,
            derived_from_field_id=derived_from_field_id,
        )

    # ── derivation hook ──

    def record_derived_field(
        self,
        case_id: str,
        field_id: str,
        value: Any,
        *,
        derived_from_field_id: str,
        source_message_id: Optional[str] = None,
    ) -> int:
        """Record a field the agent worked out from another recorded answer.

        This is the shape a derivation takes: the derived field carries
        ``origin='derived'``, names the field it came from, and INHERITS
        that field's ``source_message_id`` when the caller does not supply
        one — so the provenance chain leads back to the message the user
        actually wrote, not to the turn the agent happened to reason on.

        Raises KeyError if the source field is not recorded: you cannot
        derive from an answer you do not have.
        """
        if source_message_id is None:
            record = self.get_case(case_id)
            if record is None:
                raise KeyError(f"unknown case record: {case_id}")
            source = record.get(derived_from_field_id)
            if source is None:
                raise KeyError(
                    f"cannot derive '{field_id}': source field "
                    f"'{derived_from_field_id}' is not recorded on {case_id}"
                )
            source_message_id = source.source_message_id

        return self.record_field(
            case_id,
            field_id,
            value,
            source_message_id=source_message_id,
            origin=ORIGIN_DERIVED,
            derived_from_field_id=derived_from_field_id,
        )

    def apply_derivations(
        self,
        case_id: str,
        derivers: Mapping[str, Callable[[CaseRecord], Any]],
        *,
        overwrite: bool = False,
    ) -> List[str]:
        """Run derivation callables and record whatever they resolve.

        ``derivers`` maps a target field id to a callable that takes the
        current record and returns either ``None`` (cannot derive yet — the
        field stays empty and therefore still askable) or a
        ``(value, derived_from_field_id)`` pair.

        The callables are CLIENT-OWNED: what may be derived from what is a
        domain judgment, so it lives in the deployment, not here.  Already
        filled targets are skipped unless ``overwrite`` is set — a derived
        answer never silently stomps a stated one.

        Returns the field ids that were recorded.
        """
        recorded: List[str] = []
        for target_id, deriver in derivers.items():
            record = self.get_case(case_id)
            if record is None:
                raise KeyError(f"unknown case record: {case_id}")
            if record.is_filled(target_id) and not overwrite:
                continue
            resolved = deriver(record)
            if resolved is None:
                continue
            value, source_field_id = resolved
            self.record_derived_field(
                case_id,
                target_id,
                value,
                derived_from_field_id=source_field_id,
            )
            recorded.append(target_id)
        return recorded

    # ── intake surface (convenience passthrough) ──

    def empty_required_fields(
        self,
        case_id: str,
        field_set: FieldSet,
        *,
        include_unaskable: bool = False,
    ) -> List[FieldSpec]:
        record = self.get_case(case_id)
        if record is None:
            raise KeyError(f"unknown case record: {case_id}")
        return empty_required_fields(
            record, field_set, include_unaskable=include_unaskable
        )


def safe_record_field(store: CaseRecordStore, *args: Any, **kwargs: Any) -> Optional[int]:
    """Best-effort field write that NEVER raises.

    Mirrors the observability path's swallow contract: recording is not
    allowed to break the live agent loop or its reply.  Callers that WANT
    the error (tests, CLI tooling, phase-2 intake) call ``record_field``.
    """
    try:
        return store.record_field(*args, **kwargs)
    except Exception:  # noqa: BLE001 - intentional swallow (recording only)
        return None


__all__ = [
    "ORIGIN_USER_STATED",
    "ORIGIN_DERIVED",
    "VALID_ORIGINS",
    "STATUS_OPEN",
    "STATUS_SUPERSEDED",
    "CaseFieldValue",
    "CaseRecord",
    "FieldSpec",
    "FieldSet",
    "parse_field_sets",
    "load_field_sets",
    "select_field_set",
    "empty_required_fields",
    "empty_required_field_ids",
    "is_record_complete",
    "CaseRecordStore",
    "safe_record_field",
]
