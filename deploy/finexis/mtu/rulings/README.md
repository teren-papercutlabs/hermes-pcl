# MTU ruling records

Every accepted correction gets a ruling record before the source change is
considered complete. The record is the provenance authority for the
`ruling_ref` field in each typed source's `# pa-source:` header.

## Record format

Store one YAML file per ruling as `rulings/<id>.yaml`:

```yaml
id: R19
who: amelia
date: 2026-08-02
verbatim: "The exact words that accepted or stated the correction."
scope:
  knowledge_type: A
  artifacts:
    - rules/040-intake.yaml
  tags:
    - intake
    - ask-once
```

Fields are required:

- `id`: stable identifier, never reused.
- `who`: the person who made or accepted the ruling.
- `date`: the ruling date in `YYYY-MM-DD`.
- `verbatim`: exact words, not a paraphrase. If the original words cannot be
  recovered, do not invent them; keep the affected source `status: unverified`
  until the gap is resolved.
- `scope`: the typed knowledge class, every affected source artifact, and the
  eval tags whose behavior may change.

The typed source header references the record by id:

```yaml
# ruling_ref:
# - R19
```

Legacy `R01`–`R18` references are mapped in
`../evidence/correction-record.md`. New corrections use the per-file format
above. A source may list multiple ids when several rulings shaped the same
artifact.
