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
import logging
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


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


# ── The value contract ──────────────────────────────────────────────────
#
# A field may declare a VALUE CONTRACT: a promise about the shape of what it
# holds.  The contract is enforced ONCE, at the record write, because the
# record is what every downstream reader — the prompt, a suitability
# computation, an approved sentence's slot, an eval — actually reads.
# Enforced anywhere further downstream, each reader re-derives the same
# mapping from the same free text, and they drift apart one reader at a time.
#
# Three KINDS, and the kinds are the generic part.  What any particular
# contract contains — which values, which patterns, which reference table —
# is deployment config and never appears in this module.
#
#   enum     — a closed set of canonical values, plus how free text reaches
#              one of them.
#   boolean  — the two-valued case of the same idea, stored as a real bool
#              so no reader has to decide whether "yes" and "true" differ.
#   table    — the valid set lives in a declared reference file rather than
#              in the field's own config, because it is maintained elsewhere
#              and changes on its own schedule.  Optionally KEYED by another
#              field, when the valid set depends on that field's answer.

CONTRACT_ENUM = "enum"
CONTRACT_BOOLEAN = "boolean"
CONTRACT_TABLE = "table"
VALID_CONTRACT_KINDS = (CONTRACT_ENUM, CONTRACT_BOOLEAN, CONTRACT_TABLE)

#: What an unmatched value does.  ``unpopulate`` leaves the field empty so
#: intake asks; ``accept`` keeps the value as written (the right setting when
#: a reference table is a list of KNOWN examples rather than a closed set —
#: a name missing from it is a gap in the table, not a bad answer).
UNMATCHED_UNPOPULATE = "unpopulate"
UNMATCHED_ACCEPT = "accept"

#: Which form a table-contracted value is stored in once it matches.
#: ``entry_value`` replaces the answer with the table's canonical spelling
#: (right for a field that holds an identifier and nothing else).
#: ``as_written`` keeps the answer whole and uses the match only to validate
#: (right for a descriptive field whose value carries far more than the
#: identifier — replacing it would DESTROY the premium, term, and rider
#: detail the advisor supplied).
FORM_ENTRY_VALUE = "entry_value"
FORM_AS_WRITTEN = "as_written"


# ── Canonicalisation outcomes ───────────────────────────────────────────

#: The field carries no value contract; whatever was written is the value.
CANON_UNCONTRACTED = "uncontracted"
#: The written value already satisfied the contract exactly.
CANON_EXACT = "exact"
#: The contract resolved free text to a contracted value.  ``raw_value``
#: keeps what the writer actually said.
CANON_MAPPED = "mapped"
#: A table contract matched a known entry but kept the answer as written.
CANON_VALIDATED = "validated"
#: The contract could not resolve the answer.  The field is NOT populated:
#: the row exists only to carry the raw answer and the fact that it did not
#: resolve, so the field stays in ``empty_required_fields`` and intake asks
#: again.  Never a guess, never a silent drop.
CANON_UNMAPPABLE = "unmappable"


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
    #: What the writer ACTUALLY said, before the field's contract resolved
    #: it.  Set whenever canonicalisation changed or rejected the answer, so
    #: the audit trail shows the advisor's own words next to the value the
    #: record kept.  ``None`` when the written value stood unchanged.
    raw_value: Optional[str] = None
    #: One of the ``CANON_*`` outcomes above.
    canonicalization: str = CANON_UNCONTRACTED

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
            raw_value=row.get("raw_value"),
            canonicalization=row.get("canonicalization") or CANON_UNCONTRACTED,
        )

    @property
    def is_unmappable(self) -> bool:
        """The writer answered, but the answer maps to no canonical value.

        Distinct from "never answered": the field is still empty (so intake
        asks), but the ask should be a CLARIFICATION of what was said, not a
        first request.
        """
        return self.canonicalization == CANON_UNMAPPABLE

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
class ValueTableEntry:
    """One row of a reference table: a canonical value and how to spot it."""

    value: str
    aliases: Tuple[str, ...] = ()
    patterns: Tuple[re.Pattern[str], ...] = ()
    #: Values this entry permits for a field KEYED on it.  Empty means the
    #: entry places no constraint — which is the honest answer for a product
    #: whose confirmed set nobody has recorded yet.
    keyed_values: Tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        """Does this entry's identifier appear in the written answer?

        CONTAINMENT, not equality: a field's answer is prose that carries the
        identifier plus everything else the writer said ("Wealth Voyage, 15yr
        MIP, $12k annual"), so an equality test would reject every real
        answer.  Word-boundary anchored so "Voyage" does not match inside an
        unrelated longer word.
        """
        for candidate in (self.value, *self.aliases):
            if not candidate:
                continue
            if re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", text, re.IGNORECASE):
                return True
        return any(pattern.search(text) for pattern in self.patterns)


@dataclass(frozen=True)
class ValueTable:
    """A named reference table of permitted values, loaded from config."""

    table_id: str
    entries: Tuple[ValueTableEntry, ...] = ()
    source: Optional[str] = None

    def find(self, text: str) -> Optional[ValueTableEntry]:
        for entry in self.entries:
            if entry.matches(text):
                return entry
        return None

    @property
    def values(self) -> Tuple[str, ...]:
        return tuple(entry.value for entry in self.entries)


@dataclass(frozen=True)
class ValueContract:
    """What a field promises about the values it holds.

    ONE class for all three kinds rather than a subclass tree, because the
    thing callers do with a contract is identical in every kind — hand it a
    written answer, get back a contracted value or a refusal — and the store
    must be able to hold a heterogeneous map of them without caring which is
    which.

    Every attribute here is filled from DEPLOYMENT CONFIG.  This module knows
    a field can have a closed value set, a boolean shape, or a table to check
    against; it never knows what any value means.
    """

    kind: str = CONTRACT_ENUM
    #: enum: the canonical set.  table: unused (the table supplies it).
    values: Tuple[str, ...] = ()
    #: Free text -> contracted value, tried in DECLARATION ORDER, so a
    #: deployment puts the narrower pattern first ("did not pass" before
    #: "pass").  Values here are the raw config values, coerced by kind.
    matches: Tuple[Tuple[re.Pattern[str], Any], ...] = ()
    #: table kind: which reference table to check against.
    table_id: Optional[str] = None
    #: table kind: when set, the valid set is looked up in the table by the
    #: value of THIS other field, rather than the table being the valid set.
    key_field_id: Optional[str] = None
    on_unmatched: str = UNMATCHED_UNPOPULATE
    stored_form: str = FORM_ENTRY_VALUE

    # ── the one operation ──

    def resolve(
        self,
        raw: Any,
        *,
        tables: Optional[Mapping[str, ValueTable]] = None,
        key_value: Any = None,
    ) -> Tuple[Any, str]:
        """Resolve a written answer to ``(contracted value, outcome)``.

        Returns ``(None, CANON_UNMAPPABLE)`` when nothing resolves — the
        caller must then leave the field EMPTY.  Resolving to the nearest
        plausible value would put something in the record that nobody said,
        wearing the record's authority.
        """
        if raw is None:
            return None, CANON_UNMAPPABLE
        text = str(raw).strip()
        if not text:
            return None, CANON_UNMAPPABLE
        if self.kind == CONTRACT_BOOLEAN:
            return self._resolve_boolean(raw, text)
        if self.kind == CONTRACT_TABLE:
            return self._resolve_table(text, tables or {}, key_value)
        return self._resolve_enum(text)

    # ── kinds ──

    def _resolve_enum(self, text: str) -> Tuple[Any, str]:
        lowered = text.casefold()
        for value in self.values:
            if lowered == value.casefold():
                # Already contracted (modulo case): normalise to the declared
                # spelling so the record holds exactly one form.
                return value, CANON_EXACT if text == value else CANON_MAPPED
        for pattern, value in self.matches:
            if pattern.search(text):
                return value, CANON_MAPPED
        return None, CANON_UNMAPPABLE

    def _resolve_boolean(self, raw: Any, text: str) -> Tuple[Any, str]:
        if isinstance(raw, bool):
            return raw, CANON_EXACT
        for pattern, value in self.matches:
            if pattern.search(text):
                return bool(value), CANON_MAPPED
        return None, CANON_UNMAPPABLE

    def _resolve_table(
        self,
        text: str,
        tables: Mapping[str, ValueTable],
        key_value: Any,
    ) -> Tuple[Any, str]:
        table = tables.get(str(self.table_id or ""))
        if table is None:
            # A contract naming a table nobody loaded cannot judge anything.
            # Refusing every answer here would silently block the deployment
            # on a config typo, so the field is accepted and the defect is
            # logged where an operator sees it.
            logger.warning(
                "value contract names unknown table %r; accepting as written",
                self.table_id,
            )
            return text, CANON_UNCONTRACTED

        if self.key_field_id:
            # KEYED: the table row is chosen by ANOTHER field's answer, and
            # that row's permitted values are the valid set for this field.
            if key_value is None:
                return self._unmatched(text)
            key_entry = table.find(str(key_value))
            if key_entry is None or not key_entry.keyed_values:
                # The keying answer names nothing the table knows, or the
                # table declares no constraint for it.  An UNDECLARED product
                # constrains nothing — asserting otherwise would invent a
                # restriction the deployment never ruled.
                return text, CANON_UNCONTRACTED
            sub = ValueContract(
                kind=CONTRACT_ENUM,
                values=key_entry.keyed_values,
                matches=self.matches,
            )
            value, outcome = sub._resolve_enum(text)
            if outcome == CANON_UNMAPPABLE:
                return self._unmatched(text)
            if self.stored_form == FORM_AS_WRITTEN:
                return text, CANON_VALIDATED
            return value, outcome

        entry = table.find(text)
        if entry is None:
            return self._unmatched(text)
        if self.stored_form == FORM_AS_WRITTEN:
            # Validated, not rewritten: the answer carries more than the
            # identifier and all of it is case fact.
            return text, CANON_VALIDATED
        return entry.value, (CANON_EXACT if text == entry.value else CANON_MAPPED)

    def _unmatched(self, text: str) -> Tuple[Any, str]:
        if self.on_unmatched == UNMATCHED_ACCEPT:
            return text, CANON_UNCONTRACTED
        return None, CANON_UNMAPPABLE

    # ── reporting ──

    def describe_values(
        self,
        tables: Optional[Mapping[str, ValueTable]] = None,
        *,
        key_value: Any = None,
        limit: int = 12,
    ) -> str:
        """The permitted values, for the clarification question intake asks.

        Returns "" rather than a WRONG list when the permitted set cannot be
        stated here — a keyed contract whose key is unknown, or a table too
        long to enumerate in a question.  An empty answer makes the caller ask
        without listing; a wrong list would send the advisor after values that
        were never permitted, which is worse than not listing at all.
        """
        if self.kind == CONTRACT_BOOLEAN:
            return "yes, no"
        if self.kind == CONTRACT_TABLE:
            table = (tables or {}).get(str(self.table_id or ""))
            if table is None:
                return ""
            if self.key_field_id:
                # The permitted set belongs to the KEY's row, not to the table.
                if key_value is None:
                    return ""
                entry = table.find(str(key_value))
                values: Sequence[str] = entry.keyed_values if entry else ()
            else:
                values = table.values
        else:
            values = self.values
        if not values or len(values) > limit:
            return ""
        return ", ".join(values)


#: Boolean fields accept the words humans actually write.  A deployment may
#: add its own patterns; these are the floor, so declaring `kind: boolean`
#: alone already works.
_DEFAULT_BOOLEAN_MATCHES: Tuple[Tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"(?i)^\s*(yes|y|true|t|1)\s*$"), True),
    (re.compile(r"(?i)^\s*(no|n|false|f|0)\s*$"), False),
)


def _compile_matches(
    raw: Any,
    field_id: str,
    *,
    kind: str,
    values: Sequence[str],
) -> Tuple[Tuple[re.Pattern[str], Any], ...]:
    matches: List[Tuple[re.Pattern[str], Any]] = []
    for item in raw or ():
        if not isinstance(item, Mapping):
            raise ValueError(
                f"field '{field_id}': value_contract match entries must be mappings"
            )
        pattern = str(item.get("pattern") or "")
        value = item.get("value")
        if not pattern or value is None:
            raise ValueError(
                f"field '{field_id}': value_contract match needs pattern and value"
            )
        if kind == CONTRACT_BOOLEAN:
            resolved: Any = bool(value)
        else:
            resolved = str(value)
            if values and resolved not in values:
                # A mapping that produces a value outside the declared set
                # defeats the whole point of declaring the set.
                raise ValueError(
                    f"field '{field_id}': value_contract match maps to "
                    f"{resolved!r}, which is not one of the declared values"
                )
        try:
            matches.append((re.compile(pattern), resolved))
        except re.error as exc:
            raise ValueError(
                f"field '{field_id}': value_contract bad pattern {pattern!r}: {exc}"
            ) from exc
    return tuple(matches)


def _contract_from_mapping(raw: Any, field_id: str) -> Optional[ValueContract]:
    """Parse a field's ``value_contract:`` block, or None when it declares none."""
    if raw in (None, (), [], {}):
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"field '{field_id}': value_contract must be a mapping")
    kind = str(raw.get("kind") or CONTRACT_ENUM).strip().lower()
    if kind not in VALID_CONTRACT_KINDS:
        raise ValueError(
            f"field '{field_id}': value_contract kind must be one of "
            f"{VALID_CONTRACT_KINDS}, got {kind!r}"
        )
    on_unmatched = str(raw.get("on_unmatched") or UNMATCHED_UNPOPULATE).strip().lower()
    if on_unmatched not in (UNMATCHED_UNPOPULATE, UNMATCHED_ACCEPT):
        raise ValueError(
            f"field '{field_id}': on_unmatched must be "
            f"'{UNMATCHED_UNPOPULATE}' or '{UNMATCHED_ACCEPT}'"
        )
    stored_form = str(raw.get("stored_form") or FORM_ENTRY_VALUE).strip().lower()
    if stored_form not in (FORM_ENTRY_VALUE, FORM_AS_WRITTEN):
        raise ValueError(
            f"field '{field_id}': stored_form must be "
            f"'{FORM_ENTRY_VALUE}' or '{FORM_AS_WRITTEN}'"
        )

    values = tuple(
        str(v).strip() for v in (raw.get("values") or ()) if str(v).strip()
    )
    if kind == CONTRACT_ENUM and not values:
        raise ValueError(
            f"field '{field_id}': an enum value_contract must declare values"
        )
    table_id = raw.get("table")
    if kind == CONTRACT_TABLE and not table_id:
        raise ValueError(
            f"field '{field_id}': a table value_contract must name a table"
        )
    matches = _compile_matches(
        raw.get("match") or raw.get("matches"),
        field_id,
        kind=kind,
        values=values,
    )
    if kind == CONTRACT_BOOLEAN:
        matches = matches + _DEFAULT_BOOLEAN_MATCHES
    return ValueContract(
        kind=kind,
        values=values,
        matches=matches,
        table_id=str(table_id) if table_id else None,
        key_field_id=(
            str(raw["key_field"]) if raw.get("key_field") else None
        ),
        on_unmatched=on_unmatched,
        stored_form=stored_form,
    )


def parse_value_tables(data: Mapping[str, Any]) -> Dict[str, ValueTable]:
    """Build the reference tables a table-kind contract checks against.

    Config shape (all of it deployment vocabulary)::

        value_tables:
          <table id>:
            source: <relative path to a reference file>   # optional
            select: <key in that file holding the entries>
            value_key: <key within an entry holding the value>
            aliases_key: <key within an entry holding alternate spellings>
            entries:                                       # or declared inline
              - value: "..."
                aliases: ["..."]
                match: ["<regex>", ...]
                values: ["..."]      # permitted set when KEYED on this entry

    ``source`` is resolved by the RUNTIME (which owns the knowledge root), not
    here; this parser handles the inline form and the already-loaded rows the
    runtime hands back.
    """
    raw_tables = data.get("value_tables") or {}
    if not isinstance(raw_tables, Mapping):
        raise ValueError("'value_tables' must be a mapping of id -> definition")
    out: Dict[str, ValueTable] = {}
    for table_id, raw in raw_tables.items():
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"value table '{table_id}' must be a mapping")
        out[str(table_id)] = ValueTable(
            table_id=str(table_id),
            entries=parse_value_table_entries(
                raw.get("entries"),
                table_id=str(table_id),
                value_key=str(raw.get("value_key") or "value"),
                aliases_key=str(raw.get("aliases_key") or "aliases"),
            ),
            source=str(raw["source"]) if raw.get("source") else None,
        )
    return out


def parse_value_table_entries(
    raw: Any,
    *,
    table_id: str,
    value_key: str = "value",
    aliases_key: str = "aliases",
) -> Tuple[ValueTableEntry, ...]:
    """Turn a list of rows — inline or loaded from a reference file — into entries.

    ``value_key`` / ``aliases_key`` exist because a reference file is owned by
    whoever maintains it and names its columns its own way; the deployment
    says which column carries the value rather than the file being reshaped
    to suit this consumer.
    """
    entries: List[ValueTableEntry] = []
    for item in raw or ():
        if isinstance(item, str):
            entries.append(ValueTableEntry(value=item))
            continue
        if not isinstance(item, Mapping):
            raise ValueError(f"value table '{table_id}': entries must be mappings")
        value = item.get(value_key)
        if value is None:
            value = item.get("value") or item.get("key")
        if value is None:
            raise ValueError(
                f"value table '{table_id}': entry has no {value_key!r}: {item!r}"
            )
        patterns: List[re.Pattern[str]] = []
        for pattern in item.get("match") or ():
            try:
                patterns.append(re.compile(str(pattern)))
            except re.error as exc:
                raise ValueError(
                    f"value table '{table_id}': bad pattern {pattern!r}: {exc}"
                ) from exc
        aliases = item.get(aliases_key) or item.get("aliases") or ()
        entries.append(
            ValueTableEntry(
                value=str(value),
                aliases=tuple(str(a) for a in aliases if str(a).strip()),
                patterns=tuple(patterns),
                keyed_values=tuple(
                    str(v) for v in (item.get("values") or ()) if str(v).strip()
                ),
            )
        )
    return tuple(entries)


@dataclass(frozen=True)
class SufficiencySpec:
    """What a field's value must CARRY before the field counts as answered.

    IDENTITY IS NOT COMPLETENESS, and this is the axis that says so.

    A field like "the plan being proposed" is populated by a bare product
    name — correctly, because the name IS a stated fact and dropping it would
    lose what the writer plainly gave.  But a name is not the set of facts a
    draft needs.  Without this axis the two are indistinguishable: the field
    is filled, so it leaves the empty-required set, so the record reports the
    case as complete and tells the model to draft — off nothing but two
    product names.

    ``any_of`` is satisfied when at least one probe matches; ``all_of`` needs
    every one.  Both are named, so an unmet probe becomes a SPECIFIC question
    ("premium or sum assured") rather than re-asking the whole field.

    The probes are deployment config: what makes an answer materially
    complete is a domain judgment, and this module never makes it.
    """

    any_of: Tuple[Tuple[str, re.Pattern[str]], ...] = ()
    all_of: Tuple[Tuple[str, re.Pattern[str]], ...] = ()
    #: Human phrasing for what is still needed, when naming the probes is
    #: clumsier than one written clause.
    description: Optional[str] = None

    def missing(self, value: Any) -> List[str]:
        """Names of the probes this value does not satisfy.  Empty = complete."""
        if value is None:
            return []  # an EMPTY field is a different problem, already handled
        text = str(value)
        gaps = [name for name, pattern in self.all_of if not pattern.search(text)]
        if self.any_of and not any(
            pattern.search(text) for _name, pattern in self.any_of
        ):
            gaps.append(" or ".join(name for name, _p in self.any_of))
        return gaps

    def is_satisfied(self, value: Any) -> bool:
        return not self.missing(value)


def _sufficiency_from_mapping(raw: Any, field_id: str) -> Optional[SufficiencySpec]:
    if raw in (None, (), [], {}):
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"field '{field_id}': sufficiency must be a mapping")

    def _probes(key: str) -> Tuple[Tuple[str, re.Pattern[str]], ...]:
        out: List[Tuple[str, re.Pattern[str]]] = []
        for item in raw.get(key) or ():
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"field '{field_id}': sufficiency.{key} entries must be mappings"
                )
            name = str(item.get("name") or "").strip()
            pattern = str(item.get("pattern") or "")
            if not name or not pattern:
                raise ValueError(
                    f"field '{field_id}': sufficiency.{key} needs name and pattern"
                )
            try:
                out.append((name, re.compile(pattern)))
            except re.error as exc:
                raise ValueError(
                    f"field '{field_id}': sufficiency bad pattern {pattern!r}: {exc}"
                ) from exc
        return tuple(out)

    any_of = _probes("any_of")
    all_of = _probes("all_of")
    if not any_of and not all_of:
        raise ValueError(
            f"field '{field_id}': sufficiency declares no probes, so it can never fail"
        )
    description = raw.get("description")
    return SufficiencySpec(
        any_of=any_of,
        all_of=all_of,
        description=str(description) if description else None,
    )


@dataclass(frozen=True)
class FieldSpec:
    """One field a case type may require.

    ``askable`` is the axis that keeps the intake honest: a field the agent
    is supposed to DERIVE or fill from a standard default is still part of
    the record, but asking for it is a defect — so it never appears in the
    empty-required set even when empty.

    ``sufficiency`` is the axis that keeps the intake HONEST THE OTHER WAY:
    a field can hold a real value and still not hold enough to draft from.
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
    #: When set, the field promises a contracted value shape (enum, boolean,
    #: or checked against a reference table).  Enforced at the record write,
    #: never downstream — see ``CaseRecordStore.record_field``.
    value_contract: Optional[ValueContract] = None
    #: What the value must CARRY before the field counts as answered.  A
    #: filled-but-insufficient required field STAYS in the empty-required set,
    #: so the material-missing gate holds the draft and intake asks for the
    #: specific parts that are missing.
    sufficiency: Optional[SufficiencySpec] = None

    def sufficiency_gap(self, value: Any) -> List[str]:
        """What this value is still missing.  Empty when nothing is."""
        if self.sufficiency is None or value is None:
            return []
        return self.sufficiency.missing(value)


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
        value_contract=_contract_from_mapping(
            raw.get("value_contract") or raw.get("enum"), str(field_id)
        ),
        sufficiency=_sufficiency_from_mapping(raw.get("sufficiency"), str(field_id)),
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
                value_contract:     # optional: what the field may hold
                  kind: enum | boolean | table
                  values: [<canonical>, ...]        # enum
                  table: <value table id>           # table
                  key_field: <field id>             # table, keyed lookup
                  on_unmatched: unpopulate | accept
                  stored_form: entry_value | as_written
                  match:            # free text -> contracted, IN ORDER
                    - {pattern: "...", value: <contracted>}

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


def value_contracts_from_field_sets(
    field_sets: Mapping[str, FieldSet],
) -> Dict[str, ValueContract]:
    """Collect every contracted field id across all case types.

    A field id means the SAME thing in every case type that declares it (that
    is what makes the union safe for extraction), so its contract must agree
    too.  A conflicting second declaration is a config defect: the first wins
    and the conflict is logged rather than silently picked.
    """
    out: Dict[str, ValueContract] = {}
    for field_set in field_sets.values():
        for spec in field_set.fields:
            if spec.value_contract is None:
                continue
            existing = out.get(spec.field_id)
            if existing is None:
                out[spec.field_id] = spec.value_contract
            elif existing != spec.value_contract:
                logger.warning(
                    "field '%s' declares conflicting value contracts across "
                    "case types; keeping the first",
                    spec.field_id,
                )
    return out


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
    """Required fields still to be answered — what may still be asked.

    THIS IS THE INTAKE SURFACE.  Question generation asks for exactly these
    and nothing else; an empty list means the record is complete enough to
    build from — and the rendered block says so in as many words, so an empty
    list is a POSITIVE INSTRUCTION TO DRAFT, not merely the absence of a
    question.  That is why what counts as answered has to be right.

    A field is excluded when it is not required, when it holds a value that
    satisfies its ``sufficiency`` (whatever the origin — a DERIVED field is
    answered, so it is never re-asked), or when it is marked ``askable:
    false`` (the config says the agent must resolve it some other way, so
    asking is a defect).  Pass ``include_unaskable=True`` for the
    completeness view — everything still missing, regardless of who supplies.

    A field that is FILLED BUT INSUFFICIENT stays in this set.  A bare
    product name populates "the plan being proposed" and is a real fact worth
    keeping, but it is not the set of facts a draft needs; without the
    sufficiency check the two are indistinguishable and the record reports a
    two-product-name case as ready to draft.

    Returns FieldSpecs in field-set declaration order, so the caller can ask
    in the order the config intends.
    """
    out: List[FieldSpec] = []
    for spec in field_set.fields:
        if not spec.required:
            continue
        if record.is_filled(spec.field_id) and not spec.sufficiency_gap(
            record.value_of(spec.field_id)
        ):
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

    ``value_contracts`` makes this store the ONE place a field's value
    contract is enforced.  Every write — stated, derived, or config-derivation
    — funnels through ``record_field``, so a record built by this store holds
    contracted values only, and no downstream reader has to know the mapping.
    A store constructed without them (read-only callers) enforces nothing,
    which is correct: it never writes.
    """

    def __init__(
        self,
        session_db: Any,
        *,
        value_contracts: Optional[Mapping[str, ValueContract]] = None,
        value_tables: Optional[Mapping[str, ValueTable]] = None,
    ):
        self._db = session_db
        self._contracts: Dict[str, ValueContract] = dict(value_contracts or {})
        self._tables: Dict[str, ValueTable] = dict(value_tables or {})

    # ── the value contract ──

    def value_contract(self, field_id: str) -> Optional[ValueContract]:
        return self._contracts.get(field_id)

    @property
    def value_tables(self) -> Mapping[str, ValueTable]:
        return self._tables

    def canonicalize(
        self,
        field_id: str,
        value: Any,
        *,
        record: Optional[CaseRecord] = None,
    ) -> Tuple[Any, str]:
        """``(contracted value, outcome)`` for a value about to be written.

        Exposed so a caller can compare a candidate answer against what the
        record already holds WITHOUT writing — an advisor restating
        "aggressive" against a stored "Aggressive" has changed nothing, and
        churning the record version on it would be a lie about the case.

        ``record`` supplies the KEYING answer for a contract whose valid set
        depends on another field (a minimum investment period is only valid
        against the product that offers it).
        """
        contract = self._contracts.get(field_id)
        if contract is None:
            return value, CANON_UNCONTRACTED
        key_value = None
        if contract.key_field_id and record is not None:
            key_value = record.value_of(contract.key_field_id)
        return contract.resolve(value, tables=self._tables, key_value=key_value)

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

        **THIS IS WHERE A FIELD'S VALUE CONTRACT IS ENFORCED**, because this
        is where the field is POPULATED.  A contracted field stores exactly
        one of the values its contract permits; the writer's own wording is
        preserved beside it as ``raw_value``, so the audit trail shows what
        the advisor actually said.

        An answer the contract cannot resolve does NOT populate the field.
        The row is written with a null value and ``canonicalization =
        unmappable``, which keeps the field in ``empty_required_fields`` —
        so intake asks a clarification before anything is drafted.  Resolving
        it to the nearest value would be a guess wearing the record's
        authority, and a guess is the one thing this substrate must never
        store.
        """
        if origin not in VALID_ORIGINS:
            raise ValueError(
                f"origin must be one of {VALID_ORIGINS}, got {origin!r}"
            )
        if origin == ORIGIN_DERIVED and not derived_from_field_id:
            raise ValueError(
                "a derived field must name derived_from_field_id"
            )
        contract = self._contracts.get(field_id)
        keying_record: Optional[CaseRecord] = None
        if contract is not None and contract.key_field_id:
            keying_record = self.get_case(case_id)
        canonical, outcome = self.canonicalize(
            field_id, value, record=keying_record
        )
        raw_value: Optional[str] = None
        if outcome in (CANON_MAPPED, CANON_VALIDATED, CANON_UNMAPPABLE):
            raw_value = None if value is None else str(value)
        if outcome == CANON_UNMAPPABLE:
            logger.info(
                "case %s: field '%s' answer %r satisfies no contracted value; "
                "leaving it empty so intake asks",
                case_id,
                field_id,
                value,
            )
        return self._db.record_pa_case_field(
            case_id=case_id,
            field_id=field_id,
            value=canonical,
            origin=origin,
            source_message_id=source_message_id,
            derived_from_field_id=derived_from_field_id,
            raw_value=raw_value,
            canonicalization=outcome,
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
    "CANON_UNCONTRACTED",
    "CANON_EXACT",
    "CANON_MAPPED",
    "CANON_VALIDATED",
    "CANON_UNMAPPABLE",
    "CONTRACT_ENUM",
    "CONTRACT_BOOLEAN",
    "CONTRACT_TABLE",
    "VALID_CONTRACT_KINDS",
    "UNMATCHED_ACCEPT",
    "UNMATCHED_UNPOPULATE",
    "FORM_AS_WRITTEN",
    "FORM_ENTRY_VALUE",
    "CaseFieldValue",
    "CaseRecord",
    "ValueContract",
    "ValueTable",
    "ValueTableEntry",
    "SufficiencySpec",
    "FieldSpec",
    "FieldSet",
    "value_contracts_from_field_sets",
    "parse_value_tables",
    "parse_value_table_entries",
    "parse_field_sets",
    "load_field_sets",
    "select_field_set",
    "empty_required_fields",
    "empty_required_field_ids",
    "is_record_complete",
    "CaseRecordStore",
    "safe_record_field",
]
