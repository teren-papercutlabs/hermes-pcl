"""Read-only iLinked WC/status adapters for Christopher.

The module is intentionally data-backed. It reads captured iLinked corpus files
or detail fixtures and returns normalized JSON for Hermes' configured custom
operations. It never opens a browser and never submits anything to iLinked.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_CORPUS_GLOBS = (
    "~/pcl/ilinked-corpus/tgg/full-import-*",
    "/home/pclaw/pcl/ilinked-corpus/tgg/full-import-*",
    "/home/pclaw/ilinked-corpus/tgg/full-import-*",
    "/home/pclaw/.christopher/ilinked-corpus/full-import-*",
    "/home/pclaw/.christopher/ilinked-corpus/current",
    "/Users/pcloffice/pcl/ilinked-corpus/tgg/full-import-*",
)

DEFAULT_DETAIL_FIXTURES = (
    "/home/pclaw/.christopher/ilinked-corpus/detail-work-costing.json",
    "/home/pclaw/apps/hermes-pcl/tests/fixtures/clients/tgg/ilinked/detail-work-costing.json",
    "tests/fixtures/clients/tgg/ilinked/detail-work-costing.json",
    "/Users/pcloffice/pcl-biz/_agents/edna/specs/2026-05-23-tgg-content-inventory/cli-samples/detail-work-costing.json",
)

_DATE_FORMATS = (
    "%d %b %Y",
    "%d-%b-%Y",
    "%d %B %Y",
    "%Y-%m-%d",
)


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be a JSON object")
    return parsed


def _first(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _upper(value: Any) -> str:
    return _norm(value).upper()


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _candidate_detail_files() -> list[Path]:
    files: list[Path] = []
    explicit = os.getenv("CHRISTOPHER_ILINKED_DETAIL_JSON", "").strip()
    if explicit:
        files.append(Path(explicit).expanduser())
    detail_dir = os.getenv("CHRISTOPHER_ILINKED_DETAIL_DIR", "").strip()
    if detail_dir:
        files.extend(sorted(Path(detail_dir).expanduser().glob("*.json")))
    files.extend(Path(p) for p in DEFAULT_DETAIL_FIXTURES)
    seen: set[str] = set()
    out: list[Path] = []
    for path in files:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists() and path.is_file():
            out.append(path)
    return out


def _candidate_corpus_dirs() -> list[Path]:
    dirs: list[Path] = []
    explicit = os.getenv("CHRISTOPHER_ILINKED_CORPUS_DIR", "").strip()
    if explicit:
        dirs.append(Path(explicit).expanduser())
    for pattern in DEFAULT_CORPUS_GLOBS:
        dirs.extend(Path(p) for p in glob.glob(os.path.expanduser(pattern)))
    seen: set[str] = set()
    existing: list[Path] = []
    for path in dirs:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists() and path.is_dir():
            existing.append(path)
    return sorted(existing, key=lambda p: p.stat().st_mtime, reverse=True)


def _iter_detail_envelopes() -> Iterable[tuple[Path, dict[str, Any]]]:
    for path in _candidate_detail_files():
        try:
            parsed = _load_json(path)
        except Exception:
            continue
        if isinstance(parsed, dict):
            data = parsed.get("data")
            if isinstance(data, dict):
                yield path, data
            else:
                yield path, parsed


def _detail_values(detail: Mapping[str, Any]) -> dict[str, Any]:
    matched = detail.get("matched_row")
    if isinstance(matched, Mapping):
        values = matched.get("values")
        if isinstance(values, Mapping):
            return dict(values)
    return {}


def _detail_text(detail: Mapping[str, Any]) -> str:
    parts: list[str] = []
    parts.append(str(detail.get("identifier") or ""))
    values = _detail_values(detail)
    parts.extend(str(v) for v in values.values())
    sections = detail.get("sections")
    if isinstance(sections, Mapping):
        parts.extend(str(v) for v in sections.values())
    return " ".join(parts)


def _find_detail(query: str) -> tuple[Path, dict[str, Any]] | None:
    needle = _upper(query)
    if not needle:
        return None
    for path, detail in _iter_detail_envelopes():
        if needle in _upper(_detail_text(detail)):
            return path, detail
    return None


def _iter_index_rows() -> Iterable[tuple[Path, dict[str, Any]]]:
    for root in _candidate_corpus_dirs():
        for rel in ("task-index-date-desc.json", "task-index.json"):
            path = root / rel
            if not path.exists():
                continue
            try:
                parsed = _load_json(path)
            except Exception:
                continue
            if isinstance(parsed, list):
                for row in parsed:
                    if isinstance(row, dict):
                        yield path, row


def _row_cells(row: Mapping[str, Any]) -> list[str]:
    cells = row.get("cells")
    return [str(cell or "") for cell in cells] if isinstance(cells, list) else []


def _row_text(row: Mapping[str, Any]) -> str:
    return " ".join([str(row.get("task_key") or ""), str(row.get("leaf_text") or ""), *_row_cells(row)])


def _find_index_rows(query: str) -> list[tuple[Path, dict[str, Any]]]:
    needle = _upper(query)
    if not needle:
        return []
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path, row in _iter_index_rows():
        if needle in _upper(_row_text(row)):
            matches.append((path, row))
    return matches


def _field(values: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None and _norm(value):
            return _norm(value)
    return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = _norm(value)
    # Strip detail prose like "Ng ... on 22-May-2026 09:12:21 AM".
    match = re.search(r"\b\d{1,2}[- ][A-Za-z]{3,9}[- ]\d{4}\b", raw)
    candidate = match.group(0) if match else raw
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _days_outstanding(date_text: str | None) -> int | None:
    dt = _parse_date(date_text)
    if not dt:
        return None
    return (datetime.now(timezone.utc).date() - dt.date()).days


def _cost_lines(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    tables = detail.get("tables")
    if not isinstance(tables, list):
        return []
    lines: list[dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, Mapping):
            continue
        rows = table.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) < 10:
                continue
            cells = [str(cell or "").strip() for cell in row]
            # The iLinked WC grid has three leading layout columns before No.
            # Sample row shape: '', '', '', '1', 'Service', description, unit,
            # unit_cost, qty, total, ..., job_code, vendor, contract, work_order.
            no_idx = next((i for i, cell in enumerate(cells[:6]) if cell.isdigit()), -1)
            if no_idx < 0 or no_idx + 6 >= len(cells):
                continue
            line = {
                "no": cells[no_idx],
                "type": cells[no_idx + 1] or None,
                "description": cells[no_idx + 2] or None,
                "unit": cells[no_idx + 3] or None,
                "unit_cost": cells[no_idx + 4] or None,
                "quantity": cells[no_idx + 5] or None,
                "total": cells[no_idx + 6] or None,
            }
            tail = cells[no_idx + 7 :]
            if len(tail) >= 4:
                line.update(
                    {
                        "job_code": tail[-4] or None,
                        "vendor": tail[-3] or None,
                        "contract": tail[-2] or None,
                        "work_order": tail[-1] or None,
                    }
                )
            lines.append(line)
    # Dedupe repeated tables by line number + description + total.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for line in lines:
        key = (line.get("no"), line.get("description"), line.get("total"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return deduped


def _wc_from_detail(query: str, source: Path, detail: Mapping[str, Any]) -> dict[str, Any]:
    values = _detail_values(detail)
    lines = _cost_lines(detail)
    vendors = sorted({str(line.get("vendor")) for line in lines if line.get("vendor")})
    contracts = sorted({str(line.get("contract")) for line in lines if line.get("contract")})
    return {
        "ok": True,
        "kind": "ilinked_wc_lookup",
        "query": query,
        "work_costing_id": _field(values, "Work Costing Number") or detail.get("identifier"),
        "job_no": _field(values, "Job Number"),
        "location": _field(values, "Location"),
        "work_description": _field(values, "Work Costing Description", "Description"),
        "cost_lines": lines,
        "cost_line_count": len(lines),
        "total_estimate": _field(values, "Estimated WC Amount"),
        "vendor": vendors[0] if len(vendors) == 1 else None,
        "vendors": vendors,
        "contract": contracts[0] if len(contracts) == 1 else None,
        "contracts": contracts,
        "approval_status": _field(values, "Status"),
        "commencement_date": _field(values, "Commencement Date"),
        "estimated_end_date": _field(values, "Estimated End Date", "Expected Date of Completion"),
        "last_update_timestamp": _field(values, "Last Modified Date"),
        "source": {"type": "detail", "path": str(source)},
    }


def _wc_from_index(query: str, source: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    cells = _row_cells(row)
    return {
        "ok": True,
        "kind": "ilinked_wc_lookup",
        "query": query,
        "work_costing_id": cells[1] if len(cells) > 1 else row.get("task_key"),
        "job_no": None,
        "location": cells[4] if len(cells) > 4 else None,
        "work_description": cells[2] if len(cells) > 2 else None,
        "cost_lines": [],
        "cost_line_count": 0,
        "total_estimate": None,
        "vendor": None,
        "vendors": [],
        "contract": None,
        "contracts": [],
        "approval_status": cells[8] if len(cells) > 8 else None,
        "commencement_date": cells[5] if len(cells) > 5 else None,
        "estimated_end_date": row.get("date_iso"),
        "last_update_timestamp": None,
        "source": {"type": "task-index", "path": str(source)},
        "note": "index-only match; itemized WC cost lines require captured detail JSON",
    }


def ilinked_wc_lookup(payload: Mapping[str, Any]) -> dict[str, Any]:
    query = _first(
        payload,
        "job_no",
        "jobNo",
        "job",
        "task_no",
        "work_costing_no",
        "workCostingNo",
        "wc_no",
        "wcNo",
        "query",
    )
    if not query:
        raise ValueError("payload requires job_no/jobNo, work_costing_no/workCostingNo, or query")
    detail = _find_detail(query)
    if detail:
        return _wc_from_detail(query, detail[0], detail[1])
    index_matches = [m for m in _find_index_rows(query) if "WORK COSTING" in _upper(_row_text(m[1]))]
    if index_matches:
        return _wc_from_index(query, index_matches[0][0], index_matches[0][1])
    return {
        "ok": False,
        "kind": "ilinked_wc_lookup",
        "query": query,
        "error": {"code": "ILINKED_WC_NOT_FOUND", "message": f"No iLinked WC match for {query}"},
        "sources_checked": {
            "detail_files": [str(p) for p in _candidate_detail_files()],
            "corpus_dirs": [str(p) for p in _candidate_corpus_dirs()[:3]],
        },
    }


def _status_from_detail(query: str, source: Path, detail: Mapping[str, Any]) -> dict[str, Any]:
    values = _detail_values(detail)
    created = _field(values, "Created Date")
    end_date = _field(values, "Estimated End Date", "Expected Date of Completion")
    return {
        "ok": True,
        "kind": "ilinked_status",
        "query": query,
        "task_no": _field(values, "Job Number") or _field(values, "Work Costing Number") or detail.get("identifier"),
        "related_work_costing_id": _field(values, "Work Costing Number"),
        "status": _field(values, "Status"),
        "sub_status": None,
        "end_date": end_date,
        "days_outstanding": _days_outstanding(created),
        "created_date": created,
        "last_update_timestamp": _field(values, "Last Modified Date"),
        "source": {"type": "detail", "path": str(source)},
    }


def _status_from_index(query: str, source: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    cells = _row_cells(row)
    created = cells[5] if len(cells) > 5 else None
    return {
        "ok": True,
        "kind": "ilinked_status",
        "query": query,
        "task_no": cells[1] if len(cells) > 1 else row.get("task_key"),
        "related_work_costing_id": cells[1] if len(cells) > 1 and "/WC/" in cells[1] else None,
        "status": cells[8] if len(cells) > 8 else None,
        "sub_status": cells[7] if len(cells) > 7 else None,
        "end_date": row.get("date_iso"),
        "days_outstanding": _days_outstanding(created),
        "created_date": created,
        "last_update_timestamp": None,
        "location": cells[4] if len(cells) > 4 else None,
        "description": cells[2] if len(cells) > 2 else None,
        "source": {"type": "task-index", "path": str(source)},
    }


def ilinked_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    query = _first(payload, "job_no", "jobNo", "job", "task_no", "taskNo", "work_costing_no", "wc_no", "query")
    if not query:
        raise ValueError("payload requires job_no/jobNo, task_no/taskNo, work_costing_no, or query")
    detail = _find_detail(query)
    if detail:
        return _status_from_detail(query, detail[0], detail[1])
    index_matches = _find_index_rows(query)
    if index_matches:
        return _status_from_index(query, index_matches[0][0], index_matches[0][1])
    return {
        "ok": False,
        "kind": "ilinked_status",
        "query": query,
        "error": {"code": "ILINKED_STATUS_NOT_FOUND", "message": f"No iLinked task/status match for {query}"},
        "sources_checked": {
            "detail_files": [str(p) for p in _candidate_detail_files()],
            "corpus_dirs": [str(p) for p in _candidate_corpus_dirs()[:3]],
        },
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    mode = argv[0] if argv else ""
    try:
        payload = _read_payload()
        if mode in {"wc", "wc_lookup", "ilinked_wc_lookup"}:
            result = ilinked_wc_lookup(payload)
        elif mode in {"status", "ilinked_status"}:
            result = ilinked_status(payload)
        else:
            raise ValueError("first argument must be wc or status")
        print(json.dumps({"ok": result.get("ok") is not False, "data": result}))
        return 0 if result.get("ok") is not False else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "CHRISTOPHER_ILINKED_READS_ADAPTER_ERROR",
                        "message": str(exc),
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
