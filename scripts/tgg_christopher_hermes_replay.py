#!/usr/bin/env python3
"""Replay TGG WhatsApp bridge rows through the real Hermes gateway path.

This is intentionally a proof harness, not a production importer. It takes
stored WhatsApp bridge rows, rebuilds the bridge message shape, feeds them
through WhatsAppAdapter's replay debounce logic, and dispatches resulting turns
to GatewayRunner._handle_message so the normal PA session, tools, memory, and
transcript machinery are exercised.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("/tmp/tgg-christopher-replay-69533/tenants/tgg.db")
DEFAULT_CHAT = "120363403845802098@g.us"
DEFAULT_SINCE = "2026-05-24 00:00:00 SGT"
DEFAULT_SECRETS = Path.home() / ".marshal" / "secrets.env"

# Media-root remap (Problem 1): bridge_message_log.media_refs[].local_path was
# captured against a now-DELETED spec dir (.../2026-05-30-tgg-post24-ledger/
# live-bridge/media/<file>.jpg). When the harness feeds those dead paths to
# hermes, vision pre-analysis (text mode) fails -> the model gets the
# "couldn't quite see it" placeholder and never sees the image. We DO NOT
# mutate the sandbox DB rows; instead we remap at the read layer: if a media
# path doesn't exist on disk, try <MEDIA_ROOT>/<basename>. Set via the
# --media-root CLI flag or TGG_REPLAY_MEDIA_ROOT env var; any sandbox copy of
# the DB then resolves against the restored flat media dir.
_MEDIA_ROOT: "Path | None" = None
TGG_CONFIG = REPO_ROOT / "deploy" / "tgg" / "christopher" / "config.yaml"
TGG_CONSTITUTION = REPO_ROOT / "deploy" / "tgg" / "christopher" / "christopher_tgg_constitution.yaml"
DOCS_DIR = Path.home() / "pcl-docs" / "records"
LOCAL_BUSINESS_PREFIXES = ("http://127.0.0.1:", "http://localhost:")


@dataclass(frozen=True)
class ReplayProfile:
    name: str
    main_provider: str
    model: str
    transport: str
    vision_enabled: bool
    vision_provider: str | None
    vision_model: str | None
    vision_concurrency: int
    business_mode: str
    allow_prod_url: bool
    debounce_seconds: int
    direct_mention_immediate: bool
    rotate_session_every_turns: int | None = None
    require_pricing: bool = True
    # When set, force agent.image_input_mode so user-attached images route the
    # given way regardless of model capability metadata / aux-vision override.
    # "native" attaches pixels inline on the main model turn (vision-capable
    # main model, no separate vision backend / key required). "text" runs the
    # vision_analyze pre-analysis pipeline. None => auto (model-capability based).
    image_input_mode: str | None = None


REPLAY_PROFILES: dict[str, ReplayProfile] = {
    "tgg-local-gpt54-mini-gemini-vision": ReplayProfile(
        name="tgg-local-gpt54-mini-gemini-vision",
        main_provider="openai-direct-primary",
        model="gpt-5.4-mini",
        transport="codex_responses",
        vision_enabled=True,
        vision_provider="gemini",
        vision_model="gemini-3.1-flash-lite",
        vision_concurrency=8,
        business_mode="copied-db-local-operator",
        allow_prod_url=False,
        debounce_seconds=300,
        direct_mention_immediate=True,
    ),
    # Native-vision variant: same model + constitution as the gemini-vision
    # profile, but images are attached inline to the gpt-5.4-mini turn (OpenAI
    # direct) instead of being pre-analyzed by a separate gemini vision backend.
    # gpt-5.4-mini accepts image input, so the model sees the pixels directly —
    # no GEMINI_API_KEY_PCL_PA_SHARED required. This is the right path when only
    # OPENAI_API_KEY is provisioned, and it gives higher image fidelity than the
    # lossy text pre-analysis summary.
    "tgg-local-gpt54-mini-native-vision": ReplayProfile(
        name="tgg-local-gpt54-mini-native-vision",
        main_provider="openai-direct-primary",
        model="gpt-5.4-mini",
        transport="codex_responses",
        vision_enabled=True,
        vision_provider=None,
        vision_model=None,
        vision_concurrency=8,
        business_mode="copied-db-local-operator",
        allow_prod_url=False,
        debounce_seconds=300,
        direct_mention_immediate=True,
        image_input_mode="native",
        # Pricing estimation is a cost-reporting nicety, not eval-critical, and
        # the gate can spuriously trip depending on how the pricing table loads
        # in the run context vs at validation time. Per-call cost is still
        # estimated best-effort from captures during the run.
        require_pricing=False,
    ),
    "tgg-local-gemini-live": ReplayProfile(
        name="tgg-local-gemini-live",
        main_provider="gemini",
        model="gemini-3.1-flash-lite",
        transport="chat_completions",
        vision_enabled=True,
        vision_provider="gemini",
        vision_model="gemini-3.1-flash-lite",
        vision_concurrency=8,
        business_mode="copied-db-local-operator",
        allow_prod_url=False,
        debounce_seconds=300,
        direct_mention_immediate=True,
    ),
}


# ── Deployed-config-derived profiles (config-drift killer) ────────────────
# Profiles named in DERIVED_PROFILE_DELTAS take their BASE from the DEPLOYED
# christopher config (deploy/tgg/christopher/config.yaml) at resolution time
# and apply only the NAMED deltas. Everything not named inherits the deployed
# value, so deployed-config changes (vision fanout model, aux settings, new
# business operations) flow into the eval automatically instead of drifting
# against a hand-built parallel config. The static REPLAY_PROFILES above stay
# as explicit legacy variants.
DERIVED_PROFILE_DELTAS: dict[str, dict[str, Any]] = {
    # Default eval profile: the model under evaluation -> gpt-5.4-mini via
    # OpenAI direct. Vision keeps the deployed OpenAI-primary fanout.
    # Business URL/token -> eval tenant, applied at
    # the harness layer exactly as for the legacy profiles.
    "tgg-eval-gpt54-mini": {
        "main_provider": "openai-direct-primary",
        "model": "gpt-5.4-mini",
        "transport": "codex_responses",
    },
}


def _deployed_profile_base() -> dict[str, Any]:
    """Derive replay-profile base values from the DEPLOYED christopher config.

    The deployed config is the single source of truth for what christopher
    actually runs (main provider/model, vision fanout). A vision provider of
    "main" in the deployed auxiliary section resolves to the deployed main
    provider — that IS the deployed fanout shape.
    """
    config = _load_yaml(TGG_CONFIG)
    model_cfg = config.get("model") or {}
    aux = config.get("auxiliary") or {}
    vision = aux.get("vision") if isinstance(aux, dict) else {}
    vision = vision if isinstance(vision, dict) else {}

    main_provider = str(model_cfg.get("provider") or "gemini")
    main_model = str(model_cfg.get("default") or "gemini-3.1-flash-lite")
    vision_provider = str(vision.get("provider") or "main")
    if vision_provider == "main":
        vision_provider = main_provider
    vision_model = str(vision.get("model") or main_model)

    return {
        "main_provider": main_provider,
        "model": main_model,
        "transport": "chat_completions" if main_provider == "gemini" else "codex_responses",
        "vision_enabled": True,
        "vision_provider": vision_provider,
        "vision_model": vision_model,
        "vision_concurrency": 8,
        # Harness-level (NOT deployed-config) settings — eval tenant backend,
        # replay debounce shape. Same values the legacy profiles use.
        "business_mode": "copied-db-local-operator",
        "allow_prod_url": False,
        "debounce_seconds": 300,
        "direct_mention_immediate": True,
    }


def _build_derived_profile(name: str) -> ReplayProfile:
    deltas = DERIVED_PROFILE_DELTAS[name]
    base = _deployed_profile_base()
    base.update(deltas)
    return ReplayProfile(name=name, **base)


def _replay_profile_names() -> list[str]:
    return sorted({*REPLAY_PROFILES, *DERIVED_PROFILE_DELTAS})


def _infer_vision_provider(vision_provider: str | None, vision_model: str | None) -> str | None:
    provider = (vision_provider or "").strip()
    if provider:
        return provider
    model = (vision_model or "").strip().lower()
    if model.startswith("gemini-") or model.startswith("google/"):
        return "gemini"
    return None


def _validate_provider_model_args(*, vision_provider: str | None, vision_model: str | None) -> None:
    model = (vision_model or "").strip().lower()
    provider = (_infer_vision_provider(vision_provider, vision_model) or "main").strip().lower()
    if model.startswith("gemini-") and provider not in {"gemini", "google", "google-gemini", "google-ai-studio"}:
        raise ValueError(
            f"vision model {vision_model!r} requires a Gemini vision provider; got {provider!r}"
        )


def _resolve_replay_profile(args: argparse.Namespace) -> ReplayProfile:
    profile_name = str(args.profile or "")
    if profile_name in DERIVED_PROFILE_DELTAS:
        base = _build_derived_profile(profile_name)
    else:
        base = REPLAY_PROFILES.get(profile_name)
    if base is None:
        known = ", ".join(_replay_profile_names())
        raise SystemExit(f"Unknown replay profile {args.profile!r}. Known profiles: {known}")
    vision_provider = args.vision_provider if args.vision_provider is not None else base.vision_provider
    vision_model = args.vision_model if args.vision_model is not None else base.vision_model
    return ReplayProfile(
        name=base.name,
        main_provider=base.main_provider,
        model=args.model if args.model is not None else base.model,
        transport=base.transport,
        vision_enabled=base.vision_enabled,
        vision_provider=_infer_vision_provider(vision_provider, vision_model),
        vision_model=vision_model,
        vision_concurrency=args.vision_concurrency if args.vision_concurrency is not None else base.vision_concurrency,
        business_mode="external-local-operator" if args.no_local_operator_backend else base.business_mode,
        allow_prod_url=base.allow_prod_url,
        debounce_seconds=args.debounce_seconds if args.debounce_seconds is not None else base.debounce_seconds,
        direct_mention_immediate=base.direct_mention_immediate,
        rotate_session_every_turns=(
            args.rotate_session_every_turns
            if args.rotate_session_every_turns is not None
            else base.rotate_session_every_turns
        ),
        require_pricing=base.require_pricing,
        image_input_mode=base.image_input_mode,
    )


@dataclass
class ReplayRecord:
    source_ref: str
    chat_jid: str
    chat_name: str
    sender_id: str
    ts: int
    sgt: str
    text: str
    message_kind: str
    has_media: bool
    media_refs: list[dict[str, Any]]
    quoted_text: str
    reply_to_source_ref: str
    raw_json: dict[str, Any]


@dataclass
class PublishedTurn:
    turn_id: str
    event: Any
    segment: list[dict[str, Any]]
    source_refs: list[str]
    session_id: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    estimated_cost_usd: float
    llm_call_count: int
    llm_calls: list[dict[str, Any]]
    model: str
    provider: str
    assistant: str


def _load_secrets(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _mint_local_christopher_token(ps_data_dir: Path) -> str | None:
    spine = ps_data_dir / "spine.db"
    if not spine.exists():
        return None
    import sqlite3

    token = f"pcl_pa_tgg_christopher_replay"
    scopes = json.dumps(
        [
            "cases:read",
            "cases:write",
            "observations:write",
            "attention:write",
            "state:write",
            "christopher:read",
            "christopher:write",
            "agent-config:read",
            "agent-config:write",
        ]
    )
    with sqlite3.connect(spine) as conn:
        conn.execute(
            """
            INSERT INTO ps_service_tokens
              (token, tenant_slug, agent_name, scopes_json, created_at, revoked_at)
            VALUES (?, 'tgg', 'christopher', ?, strftime('%s','now'), NULL)
            ON CONFLICT(token) DO UPDATE SET
              scopes_json = excluded.scopes_json,
              revoked_at = NULL
            """,
            (token, scopes),
        )
        conn.commit()
    return token


def _prepare_env(hermes_home: Path, *, secrets: Path, live_business_writes: bool = False) -> None:
    _load_secrets(secrets)
    os.environ.setdefault("PS_DATA_DIR", str(Path(DEFAULT_DB).parent.parent))
    # Christopher's deploy config expects this name. Studio secrets currently
    # still carry a legacy Bobby token, but local replay can mint a
    # Christopher-scoped token against the copied PS database.
    if "CHRISTOPHER_TGG_PS_SERVICE_TOKEN" not in os.environ:
        ps_data_dir = os.environ.get("PS_DATA_DIR")
        local = _mint_local_christopher_token(Path(ps_data_dir)) if ps_data_dir else None
        if local:
            os.environ["CHRISTOPHER_TGG_PS_SERVICE_TOKEN"] = local
        else:
            legacy = os.environ.get("BOBBY_TGG_PS_SERVICE_TOKEN")
            if legacy:
                os.environ["CHRISTOPHER_TGG_PS_SERVICE_TOKEN"] = legacy
    if "GEMINI_API_KEY_PCL_PA_SHARED" not in os.environ and os.environ.get("GEMINI_API_KEY"):
        # Local Studio replay secrets currently carry the shared PA Gemini key under
        # the generic name; the deployed Christopher config references the tenant
        # shared alias. Mirror it in-process so replay uses the live config shape.
        os.environ["GEMINI_API_KEY_PCL_PA_SHARED"] = os.environ["GEMINI_API_KEY"]
    os.environ["HERMES_HOME"] = str(hermes_home)
    if live_business_writes:
        os.environ.pop("HERMES_PA_BUSINESS_DRY_RUN", None)
        os.environ.pop("HERMES_PA_AGENT_ACTION_DRY_RUN", None)
    else:
        os.environ["HERMES_PA_BUSINESS_DRY_RUN"] = "1"
        os.environ["HERMES_PA_AGENT_ACTION_DRY_RUN"] = "1"
    os.environ.setdefault("HERMES_OPENAI_CAPTURE_DIR", str(hermes_home / "openai-captures"))
    os.environ.setdefault("HERMES_LLM_CALL_LOG", str(hermes_home / "llm-call-ledger.jsonl"))
    os.environ["HERMES_TIMEZONE"] = "Asia/Singapore"
    os.environ.setdefault("TERMINAL_CWD", str(REPO_ROOT))


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not load as a mapping")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _validate_replay_args(args: argparse.Namespace) -> None:
    profile = _resolve_replay_profile(args)
    vision_model = str(profile.vision_model or "").strip().lower()
    vision_provider = str(profile.vision_provider or "main").strip().lower()
    if vision_model.startswith("gemini") and vision_provider not in {"gemini", "google", "google-gemini", "google-ai-studio"}:
        raise SystemExit(
            "--vision-model looks like Gemini but --vision-provider points elsewhere. "
            "Refusing to route a Gemini model name through the main provider."
        )
    external_business_url = bool(
        args.business_base_url
        and not str(args.business_base_url).startswith(LOCAL_BUSINESS_PREFIXES)
    )
    if external_business_url and not profile.allow_prod_url and not args.prod_pilot_run_id:
        raise SystemExit(
            "--business-base-url must be localhost/127.0.0.1 for replay. "
            "Use the local copied-DB backend, or pass --prod-pilot-run-id to "
            "enter the bounded production pilot write path."
        )
    if args.prod_pilot_run_id:
        if not args.no_local_operator_backend or not args.business_base_url:
            raise SystemExit("--prod-pilot-run-id requires --no-local-operator-backend and --business-base-url")
        if not args.live_business_writes:
            raise SystemExit("--prod-pilot-run-id requires --live-business-writes")
    if not Path(args.db).exists():
        raise SystemExit(f"Replay DB does not exist: {args.db}")
    sidecars = [Path(str(args.db) + suffix) for suffix in ("-wal", "-shm")]
    existing_sidecars = [str(path) for path in sidecars if path.exists()]
    if existing_sidecars:
        raise SystemExit(
            "Replay DB has SQLite sidecars. Restore the copied DB cleanly before replay: "
            + ", ".join(existing_sidecars)
        )
    with sqlite3.connect(f"file:{Path(args.db)}?mode=ro", uri=True) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    if not result or str(result[0]).lower() != "ok":
        raise SystemExit(f"Replay DB integrity check failed: {result[0] if result else 'no result'}")
    if profile.business_mode != "copied-db-local-operator" and not args.business_base_url:
        raise SystemExit(f"Replay profile {profile.name} requires an explicit business bridge URL")
    if profile.require_pricing:
        for provider, model in (
            (profile.main_provider, profile.model),
            (profile.vision_provider, profile.vision_model),
        ):
            if not model:
                continue
            if _estimate_cost_for_usage(
                model=model,
                provider=provider,
                input_total=1,
                cached_input=0,
                output_tokens=1,
            ) <= 0:
                raise SystemExit(f"No pricing configured for {provider or 'main'} / {model}")


def _normalize_job_no(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value if str(v).strip())
        text = str(value or "").strip()
        if text:
            return text
    return None


def _split_address(value: str) -> tuple[str | None, str | None, str | None]:
    text = " ".join(str(value or "").split())
    block = None
    street = None
    unit = None
    block_match = re.search(r"\b(?:BLK|BLOCK)\s+([A-Z0-9]+)", text, flags=re.I)
    unit_match = re.search(r"#\s*([0-9]{1,3}\s*-\s*[0-9A-Z]+)", text, flags=re.I)
    if block_match:
        block = block_match.group(1).upper()
    if unit_match:
        unit = "#" + re.sub(r"\s+", "", unit_match.group(1)).upper()
    if block_match:
        street_end = unit_match.start() if unit_match else len(text)
        street = text[block_match.end():street_end].strip(" ,")
    return block, street or None, unit


def _norm_addr_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _assert_observation_address_matches(job_no: str, case: dict[str, Any], payload: dict[str, Any]) -> None:
    """Reject an observation whose stated address materially disagrees with the resolved
    case's address. In dense WhatsApp turn bundles the model occasionally pairs the right
    completion text with the wrong job number (e.g. a 986C #04-90 basin completion tagged
    onto a Blk 25 Hougang case). The tool knows the case address at attach time; refuse the
    mismatch so the agent re-resolves the job number instead of silently writing to the wrong
    case. Acts only on positive evidence: both sides present and normalized-different."""
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    stated_address = _first_nonempty(fields.get("address"), payload.get("address"))
    split_block, _split_street, split_unit = _split_address(stated_address or "")
    stated_block = _first_nonempty(fields.get("block"), payload.get("block"), split_block)
    stated_unit = _first_nonempty(fields.get("unit"), payload.get("unit"), split_unit)
    case_block = case.get("block")
    case_unit = case.get("unit")
    if stated_block and case_block and _norm_addr_token(stated_block) != _norm_addr_token(case_block):
        raise ValueError(
            f"address mismatch for {job_no}: this case is block {case_block} "
            f"(unit {case_unit or 'n/a'}), but the observation states block {stated_block} "
            f"(unit {stated_unit or 'n/a'}). Re-resolve the job number for the stated address "
            f"before recording -- do not attach a completion to the wrong case."
        )
    same_or_unknown_block = (
        (not stated_block) or (not case_block) or (_norm_addr_token(stated_block) == _norm_addr_token(case_block))
    )
    if stated_unit and case_unit and same_or_unknown_block and _norm_addr_token(stated_unit) != _norm_addr_token(case_unit):
        raise ValueError(
            f"address mismatch for {job_no}: this case is unit {case_unit} "
            f"(block {case_block or 'n/a'}), but the observation states unit {stated_unit} "
            f"(block {stated_block or 'n/a'}). Re-resolve the job number for the stated unit "
            f"before recording -- do not attach a completion to the wrong case."
        )


def _case_row_to_api(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "jobNo": row["job_no"],
        "job_no": row["job_no"],
        "wcNo": row["wc_no"],
        "wc_no": row["wc_no"],
        "zone": row["zone"],
        "state": row["state"],
        "source": row["source"],
        "serviceLine": row["service_line"],
        "service_line": row["service_line"],
        "address": row["address"],
        "block": row["block"],
        "unit": row["unit"],
        "streetName": row["street_name"],
        "street_name": row["street_name"],
        "problem": row["problem"],
        "feedback": row["feedback"],
        "typeOfWork": row["type_of_work"],
        "type_of_work": row["type_of_work"],
        "jobStatus": row["job_status"],
        "job_status": row["job_status"],
        "linkfmStatus": row["linkfm_status"],
        "linkfm_status": row["linkfm_status"],
    }


def _case_search_tokens(search: str) -> list[str]:
    stop = {"BLK", "BLOCK", "THE", "AND", "FOR", "TO", "A", "AN", "OF", "WORK", "CAST", "REQUEST"}
    seen: set[str] = set()
    out: list[str] = []
    for token in re.findall(r"[A-Z0-9#]+", str(search or "").upper()):
        if len(token) < 2 or token in stop:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _case_search_anchors(search: str) -> tuple[str | None, str | None]:
    text = str(search or "")
    block_match = re.search(r"\b(?:BLK|BLOCK)\s+([A-Z0-9]+)", text, flags=re.I)
    unit_match = re.search(r"#\s*([0-9]{1,3}\s*-\s*[0-9A-Z]+)", text, flags=re.I)
    block = block_match.group(1).upper() if block_match else None
    # Unit anchor is normalized WITHOUT the '#': stored units are inconsistent
    # ('11-109' from master imports vs '#11-109' from WA-created cases), so all
    # unit comparisons strip '#' + spaces on both sides. A '#'-prefixed anchor
    # hard-filtered every master-seeded case to zero results (2026-06-10 day-25
    # finding: SK/JOB/2604/2376 existed, search returned [], Christopher minted
    # a WA-only duplicate).
    unit = re.sub(r"\s+", "", unit_match.group(1)).upper() if unit_match else None
    return block, unit


def _next_wa_job_no(conn: sqlite3.Connection, ts: int) -> str:
    stamp = datetime.fromtimestamp(ts)
    prefix = f"WA/JOB/{stamp:%y%m}/"
    rows = conn.execute(
        "SELECT job_no FROM cases WHERE job_no LIKE ? ORDER BY id DESC LIMIT 200",
        (f"{prefix}%",),
    ).fetchall()
    max_suffix = 0
    for row in rows:
        suffix = str(row[0] or "")[len(prefix):]
        if suffix.isdigit():
            max_suffix = max(max_suffix, int(suffix))
    return f"{prefix}{max_suffix + 1:04d}"


class _ReplayOperatorBackend:
    """Local operator API with the same route shape as the TGG portal.

    DEPRECATED (2026-06-10, WB b7e19b21): this hand-written python stand-in drifted
    from the deployed systems API (a '#' unit-normalization gap made Christopher
    falsely mint duplicate cases in eval). The canonical replay backend is now the
    isolated EVAL TENANT served by the real deployed systems app on tgg-prod-sg
    (christopher-tgg-systems-eval.service) via --no-local-operator-backend +
    --business-base-url through an ssh tunnel — see run_replay.sh in the
    2026-06-09-christopher-wa-eval-disamb spec dir. Kept only as a fallback when
    the VPS is unreachable; do not extend its query logic.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def _handler(self):
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                backend._handle(self)

            def do_POST(self) -> None:
                backend._handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def _reply(self, handler: BaseHTTPRequestHandler, body: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def _body(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = handler.rfile.read(length).decode("utf-8")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
        try:
            if handler.command == "GET" and path == "/api/operator/cases":
                self._reply(handler, {"ok": True, "data": self._search_cases(query), "status_code": 200})
                return
            if handler.command == "GET" and path.startswith("/api/operator/cases/"):
                job_no = unquote(path.removeprefix("/api/operator/cases/").split("/", 1)[0])
                case = self._lookup_case(job_no)
                if case is None:
                    self._reply(
                        handler,
                        {"ok": False, "error": {"code": "CASE_NOT_FOUND", "message": "No case with that job number."}, "status_code": 404},
                        status=404,
                    )
                    return
                self._reply(handler, {"ok": True, "data": case, "status_code": 200})
                return
            if handler.command == "POST" and path in {"/api/operator/cases", "/api/operator/cases/create"}:
                result = self._create_case(self._body(handler))
                self._reply(handler, {"ok": True, "data": result, "status_code": 200})
                return
            if handler.command == "POST" and path.startswith("/api/operator/cases/") and path.endswith("/observations"):
                job_no = unquote(path.removeprefix("/api/operator/cases/").removesuffix("/observations"))
                result = self._add_observation(job_no, self._body(handler))
                self._reply(handler, {"ok": True, "data": result, "status_code": 200})
                return
        except Exception as exc:
            self._reply(handler, {"ok": False, "error": str(exc), "status_code": 500}, status=500)
            return
        self._reply(handler, {"ok": False, "error": "not found", "status_code": 404}, status=404)

    def _lookup_case(self, job_no: str) -> dict[str, Any] | None:
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM cases WHERE normalized_job_no = ? OR job_no = ? LIMIT 1",
                (_normalize_job_no(job_no), job_no),
            ).fetchone()
        return _case_row_to_api(row) if row else None

    def _search_cases(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        search = str(query.get("search") or "").strip()
        limit = max(1, min(int(query.get("limit") or 12), 50))
        tokens = _case_search_tokens(search)
        query_block, query_unit = _case_search_anchors(search)
        service_line = str(query.get("serviceLine") or query.get("service_line") or "").strip()
        state = str(query.get("state") or "").strip()
        source_status = str(query.get("sourceStatus") or query.get("source_status") or "").strip()
        clauses: list[str] = []
        binds: list[Any] = []
        if query_block:
            clauses.append("upper(coalesce(block, '')) = ?")
            binds.append(query_block)
        if query_unit:
            clauses.append("replace(upper(replace(coalesce(unit, ''), ' ', '')), '#', '') = ?")
            binds.append(query_unit)
        if service_line:
            clauses.append("coalesce(service_line, 'maintenance') = ?")
            binds.append(service_line)
        if state:
            clauses.append("state = ?")
            binds.append(state)
        if source_status == "wa_only":
            clauses.append("state = 'wa_only'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT * FROM cases
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 5000
                """,
                binds,
            ).fetchall()

        def score(row: sqlite3.Row) -> tuple[int, int, int]:
            haystack = " ".join(
                str(row[key] or "")
                for key in (
                    "job_no",
                    "wc_no",
                    "address",
                    "block",
                    "unit",
                    "street_name",
                    "tenant_name",
                    "problem",
                    "feedback",
                    "type_of_work",
                )
            ).upper()
            matches = sum(1 for token in tokens if token in haystack)
            anchor = 0
            block = str(row["block"] or "").upper()
            unit = str(row["unit"] or "").replace(" ", "").replace("#", "").upper()
            if query_block:
                if block != query_block:
                    return 0, 0, int(row["updated_at"] or 0)
                anchor += 20
            if query_unit:
                if unit != query_unit:
                    return 0, 0, int(row["updated_at"] or 0)
                anchor += 30
            if block and block in tokens:
                anchor += 4
            if unit and unit in tokens:
                anchor += 8
            return matches + anchor, anchor, int(row["updated_at"] or 0)

        ranked = [(score(row), row) for row in rows]
        if tokens:
            ranked = [item for item in ranked if item[0][0] > 0]
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [_case_row_to_api(row) for _, row in ranked[:limit]]

    def _create_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        zone = _first_nonempty(payload.get("zone"), evidence.get("zone"))
        address = _first_nonempty(payload.get("address"), evidence.get("address"))
        problem = _first_nonempty(payload.get("problem"), evidence.get("problem"), evidence.get("remarks"))
        source = _first_nonempty(payload.get("source"), evidence.get("source"))
        if not zone or not address or not problem or not source:
            raise ValueError("zone, address, problem, and source are required to create a new case")
        confidence = _first_nonempty(payload.get("confidence"), evidence.get("confidence"))
        if confidence and re.match(r"^(low|uncertain|guess|unknown)$", confidence, flags=re.I):
            raise ValueError("case_create requires clear WhatsApp evidence; ask for clarification instead")
        # Preserve an explicit HDB/iLinked job number as the case identity wherever the model
        # nests it. Models inconsistently pass it top-level OR under evidence.jobNo /
        # evidence.job_no / evidence.jobNoProvided; reading only the first set silently
        # dropped explicit identities and minted synthetic WA/JOB numbers (run-to-run luck).
        job_no = _first_nonempty(
            payload.get("jobNo"), payload.get("job_no"),
            evidence.get("jobNoProvided"), evidence.get("jobNo"), evidence.get("job_no"),
        )
        block, street, unit = _split_address(address or "")
        now_ts = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            if not job_no:
                job_no = _next_wa_job_no(conn, now_ts)
            norm = _normalize_job_no(job_no)
            existing = conn.execute(
                "SELECT id FROM cases WHERE normalized_job_no = ? OR job_no = ? LIMIT 1",
                (norm, job_no),
            ).fetchone()
            if existing:
                raise ValueError(f"A case with job number {job_no} already exists")
            conn.execute(
                """
                INSERT INTO cases
                  (job_no, wc_no, zone, state, priority, address, block, unit, street_name, problem,
                   feedback, contact_name, contact_phone, service_line, normalized_job_no,
                   wa_seen_at, created_at, updated_at, source)
                VALUES (?, ?, ?, 'wa_only', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'replay_wa_scratch')
                """,
                (
                    job_no,
                    payload.get("wcNo") or payload.get("wc_no"),
                    zone,
                    payload.get("priority"),
                    address,
                    block,
                    unit,
                    street,
                    problem,
                    _first_nonempty(payload.get("feedback"), payload.get("notes")),
                    _first_nonempty(payload.get("contactName"), evidence.get("contact")),
                    payload.get("contactPhone"),
                    payload.get("serviceLine") or payload.get("service_line") or "maintenance",
                    norm,
                    now_ts,
                    now_ts,
                    now_ts,
                ),
            )
            row = conn.execute("SELECT id FROM cases WHERE normalized_job_no = ?", (norm,)).fetchone()
            if row:
                conn.execute(
                    """
                    INSERT INTO case_observations
                      (case_id, source, source_ref, observed_at, fields, confidence, notes, created_at)
                    VALUES (?, 'replay_wa_scratch', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(row[0]),
                        _first_nonempty(evidence.get("source_message_id"), evidence.get("source_ref"), payload.get("sourceRef")),
                        now_ts,
                        json.dumps({"payload": payload}, ensure_ascii=False),
                        confidence or "observed",
                        _first_nonempty(payload.get("problem"), payload.get("notes")),
                        now_ts,
                    ),
                )
            conn.commit()
        return self._lookup_case(job_no) or {"jobNo": job_no}

    def _add_observation(self, job_no: str, payload: dict[str, Any]) -> dict[str, Any]:
        case = self._lookup_case(job_no)
        if not case:
            raise ValueError(f"case not found: {job_no}")
        _assert_observation_address_matches(job_no, case, payload)
        now_ts = int(time.time())
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        source_refs = fields.get("source_refs") or fields.get("sourceRefs") or payload.get("sourceRefs")
        source_ref = source_refs[0] if isinstance(source_refs, list) and source_refs else source_refs
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO case_observations
                  (case_id, source, source_ref, observed_at, fields, confidence, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(case["id"]),
                    payload.get("source") or "whatsapp",
                    str(source_ref) if source_ref else None,
                    int(payload.get("observedAt") or now_ts),
                    json.dumps(fields or payload, ensure_ascii=False),
                    payload.get("confidence"),
                    payload.get("notes"),
                    now_ts,
                ),
            )
            conn.execute("UPDATE cases SET updated_at = ?, wa_seen_at = COALESCE(wa_seen_at, ?) WHERE id = ?", (now_ts, now_ts, int(case["id"])))
            conn.commit()
        return self._lookup_case(job_no) or case


def _set_nested(mapping: dict[str, Any], keys: list[str], value: Any) -> None:
    current = mapping
    for key in keys[:-1]:
        child = current.setdefault(key, {})
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[keys[-1]] = value


def _continue_session_plan(hermes_home: Path, *, continue_session: bool) -> dict[str, Any]:
    """Session-reuse decision for a replay run (pure — trivially testable).

    Continue mode resumes the sandbox's existing hermes session the same way
    live hermes keeps one long-running session per chat: SessionStore keys
    sessions on the chat (group_sessions_per_user=False in the prepared
    config), so as long as the prior session store survives
    (sessions/sessions.json + state.db in hermes-home) AND no reset policy
    fires, get_or_create_session returns the prior entry — same session_id,
    full message history intact — and the existing ContextCompressor
    auto-compaction (threshold 0.50, aux summarize, protect-tail) fires
    naturally as multi-day history accumulates. The harness therefore only
    has to (a) NOT re-create/rotate anything, and (b) disable the wall-clock
    session reset policy (mode "none") so a daily-4am/idle-24h boundary
    crossed between replay runs cannot rotate the session.

    Returns {"resume": bool, "session_reset_mode": Optional[str], "reason": str}.
    """
    if not continue_session:
        return {
            "resume": False,
            "session_reset_mode": None,
            "reason": "fresh run (--continue-session not set)",
        }
    sessions_file = hermes_home / "sessions" / "sessions.json"
    state_db = hermes_home / "state.db"
    missing = [str(path) for path in (sessions_file, state_db) if not path.exists()]
    if missing:
        # Day-1-with-flag shape: nothing to resume yet — start fresh, but
        # still disable the reset policy so THIS session survives to the
        # next continued run.
        return {
            "resume": False,
            "session_reset_mode": "none",
            "reason": (
                "--continue-session set but no prior session store "
                f"({', '.join(missing)} missing); starting a fresh session "
                "that later --continue-session runs will resume"
            ),
        }
    return {
        "resume": True,
        "session_reset_mode": "none",
        "reason": "prior session store present; resuming the chat's most recent session",
    }


def _prepare_hermes_home(
    hermes_home: Path,
    *,
    chat_id: str,
    profile: ReplayProfile,
    business_base_url: str | None,
    prod_pilot_run_id: str | None = None,
    session_reset_mode: str | None = None,
) -> None:
    config = _load_yaml(TGG_CONFIG)
    constitution = _load_yaml(TGG_CONSTITUTION)

    provider_name = profile.main_provider
    if provider_name == "gemini":
        config["providers"] = {
            provider_name: {
                "name": "Gemini",
                "api": "https://generativelanguage.googleapis.com/v1beta",
                "key_env": "GEMINI_API_KEY_PCL_PA_SHARED",
                "default_model": profile.model,
                "transport": profile.transport,
            }
        }
        _set_nested(config, ["model", "base_url"], "https://generativelanguage.googleapis.com/v1beta")
        _set_nested(config, ["model", "api_key_source"], {"type": "env", "secrets_env_key": "GEMINI_API_KEY_PCL_PA_SHARED"})
    else:
        config["providers"] = {
            provider_name: {
                "name": "OpenAI Direct Primary",
                "api": "https://api.openai.com/v1",
                "key_env": "OPENAI_API_KEY",
                "default_model": profile.model,
                "transport": profile.transport,
            }
        }
    _set_nested(config, ["model", "provider"], provider_name)
    _set_nested(config, ["model", "default"], profile.model)
    _set_nested(config, ["agent", "profile"], "pa")
    _set_nested(config, ["agent", "max_turns"], 12)
    # Force image-input routing when the profile pins it. "native" attaches
    # pixels inline on the vision-capable main model (no separate vision
    # backend / key); "text" runs the vision_analyze pre-analysis pipeline.
    if profile.image_input_mode:
        _set_nested(config, ["agent", "image_input_mode"], profile.image_input_mode)
    _set_nested(config, ["display", "tool_progress"], "off")
    _set_nested(config, ["streaming", "enabled"], False)
    # Christopher reasons about a maintenance group as one conversation.
    # Hermes defaults to per-participant group sessions, which is right for
    # many assistants but wrong for replay/live ledger perception here.
    config["group_sessions_per_user"] = False
    config["thread_sessions_per_user"] = False
    if session_reset_mode:
        # Continue-session runs: a wall-clock daily/idle reset boundary
        # crossed between replay invocations must not rotate the resumed
        # session — multi-day history accumulation is the point.
        config["session_reset"] = {"mode": session_reset_mode, "notify": False}

    local_constitution = hermes_home / "christopher_tgg_constitution.yaml"
    _set_nested(config, ["pa", "constitution_path"], str(local_constitution))
    auxiliary = config.setdefault("auxiliary", {})
    if isinstance(auxiliary, dict):
        for value in auxiliary.values():
            if isinstance(value, dict):
                value["provider"] = "main"
                value["model"] = profile.model
        vision = auxiliary.setdefault("vision", {})
        if isinstance(vision, dict):
            vision["provider"] = profile.vision_provider or "main"
            vision["model"] = profile.vision_model or profile.model
            vision["max_concurrency"] = max(1, int(profile.vision_concurrency or 1))

    if business_base_url:
        bridge = (
            config.setdefault("pa", {})
            .setdefault("overlay", {})
            .setdefault("client", {})
            .setdefault("business_bridge", {})
        )
        operations = bridge.get("operations")
        if isinstance(operations, dict):
            base = business_base_url.rstrip("/")
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                url = str(operation.get("url") or "")
                if url.startswith("https://systems.papercut-labs.com"):
                    operation["url"] = url.replace("https://systems.papercut-labs.com", base, 1)
                if prod_pilot_run_id:
                    headers = operation.setdefault("headers", {})
                    if isinstance(headers, dict):
                        headers["X-Replay-Run-Id"] = prod_pilot_run_id

    platform = config.setdefault("platforms", {}).setdefault("whatsapp", {})
    platform["enabled"] = True
    extra = platform.setdefault("extra", {})
    extra.update(
        {
            "require_mention": True,
            "group_policy": "allowlist",
            "group_allow_from": [chat_id],
            "ingest_chats": [chat_id],
            "turn_policy": {
                chat_id: {
                    "process_all": True,
                    "debounce_seconds": profile.debounce_seconds,
                    "direct_mention_immediate": profile.direct_mention_immediate,
                }
            },
            "pa_job_type": "tgg_ops_ingest",
            "pa": {"enabled": True, "job_type": "tgg_ops_ingest"},
        }
    )

    _set_nested(constitution, ["runtime", "provider"], provider_name)
    _set_nested(constitution, ["runtime", "model"], profile.model)
    for brief in (constitution.get("job_briefs") or {}).values():
        if isinstance(brief, dict):
            runtime = brief.setdefault("runtime", {})
            runtime["provider"] = provider_name
            runtime["model"] = profile.model

    _write_yaml(hermes_home / "config.yaml", config)
    _write_yaml(local_constitution, constitution)
    (hermes_home / "sessions").mkdir(parents=True, exist_ok=True)
    (hermes_home / "cache").mkdir(parents=True, exist_ok=True)


def _parse_media_refs(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_raw_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_records(
    db_path: Path,
    *,
    chat_id: str,
    since_sgt: str,
    until_sgt: str | None,
    limit: int | None,
    skip_messages: int = 0,
    source_table: str = "bridge_message_log",
) -> list[ReplayRecord]:
    if source_table not in {"bridge_message_log", "message_ledger"}:
        raise ValueError("source_table must be bridge_message_log or message_ledger")
    clauses = ["chat_jid = ?", "sgt >= ?"]
    params: list[Any] = [chat_id, since_sgt]
    if until_sgt:
        clauses.append("sgt < ?")
        params.append(until_sgt)

    if source_table == "message_ledger":
        clauses.append("in_scope = 1")
        sql = f"""
            SELECT source_ref, chat_jid, chat_name, sender_id, ts, sgt, text,
                   message_kind, has_media, media_refs, quoted_text,
                   reply_to_source_ref, raw_json
            FROM message_ledger
            WHERE {' AND '.join(clauses)}
            ORDER BY ts, source_ref
        """
    else:
        sql = f"""
            SELECT source_ref, chat_jid, chat_name, sender_id, ts, sgt, text,
                   message_kind, has_media, media_refs, quoted_text,
                   reply_to_source_ref, raw_json
            FROM bridge_message_log
            WHERE {' AND '.join(clauses)}
            ORDER BY ts, source_ref
        """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    elif skip_messages:
        sql += " LIMIT -1"
    if skip_messages:
        sql += " OFFSET ?"
        params.append(int(skip_messages))

    rows = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(sql, params):
            rows.append(
                ReplayRecord(
                    source_ref=str(row["source_ref"] or ""),
                    chat_jid=str(row["chat_jid"] or ""),
                    chat_name=str(row["chat_name"] or row["chat_jid"] or ""),
                    sender_id=str(row["sender_id"] or ""),
                    ts=int(row["ts"] or 0),
                    sgt=str(row["sgt"] or ""),
                    text=str(row["text"] or ""),
                    message_kind=str(row["message_kind"] or "text"),
                    has_media=bool(row["has_media"]),
                    media_refs=_parse_media_refs(row["media_refs"]),
                    quoted_text=str(row["quoted_text"] or ""),
                    reply_to_source_ref=str(row["reply_to_source_ref"] or ""),
                    raw_json=_parse_raw_json(row["raw_json"]),
                )
            )
    return rows


def _remap_media_path(candidate: str) -> str:
    """Resolve a media path, remapping dead prefixes to the configured root.

    If ``candidate`` already exists on disk, it is returned unchanged. Otherwise
    — and only when a media root is configured — we try ``<MEDIA_ROOT>/<base>``
    where ``base`` is the candidate's filename. The restored media dir is flat
    and keyed by the same basenames the dead paths carry, so this resolves the
    deleted-spec-dir breakage without touching the sandbox DB rows. If neither
    exists we return the original candidate (callers/hermes report it skipped).
    """
    if not candidate:
        return candidate
    try:
        if Path(candidate).exists():
            return candidate
    except Exception:
        pass
    root = _MEDIA_ROOT
    if root is not None:
        try:
            remapped = root / Path(candidate).name
            if remapped.exists():
                return str(remapped)
        except Exception:
            pass
    return candidate


def _media_paths(refs: list[dict[str, Any]]) -> list[str]:
    paths = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        candidate = ref.get("local_path") or ref.get("path") or ref.get("file_path")
        if candidate:
            paths.append(_remap_media_path(str(candidate)))
    return paths


_REACTION_TEXT_RE = re.compile(r"^\s*\[reaction:[^\]]*\]\s*$", re.IGNORECASE)


def _is_bare_reaction_record(record: ReplayRecord) -> bool:
    """True for bare reaction messages ('[reaction: X]' / message_kind
    reaction with no other content).

    Bare reactions must never trigger replay turns — live WhatsApp turn
    formation does not fire on a thumbs-up, and a reaction-only turn gives
    the model nothing actionable. They are skipped at the replay feed."""
    if record.has_media:
        return False
    kind = (record.message_kind or "").strip().lower()
    text = (record.text or "").strip()
    if kind == "reaction":
        return not text or bool(_REACTION_TEXT_RE.match(text))
    return bool(_REACTION_TEXT_RE.match(text))


def _record_to_bridge_message(record: ReplayRecord) -> dict[str, Any]:
    raw = dict(record.raw_json)
    message_id = (
        raw.get("id")
        or str(record.source_ref).rsplit("::", 1)[-1]
        or record.source_ref
    )
    media_paths = _media_paths(record.media_refs)
    body = record.text
    message_kind = record.message_kind or ""
    if not body and record.has_media:
        body = ""
    bridge = {
        **raw,
        "messageId": message_id,
        "chatId": record.chat_jid,
        "chatName": record.chat_name,
        "senderId": record.sender_id,
        "senderName": record.sender_id.split("@", 1)[0] if record.sender_id else "",
        "isGroup": record.chat_jid.endswith("@g.us"),
        "timestamp": record.ts,
        "sgt": record.sgt,
        "body": body,
        "hasMedia": bool(record.has_media),
        "mediaType": message_kind,
        "mediaUrls": media_paths,
        "quotedText": record.quoted_text,
        "quotedMessageId": record.reply_to_source_ref,
        "fromMe": bool(raw.get("fromMe", False)),
        "_tgg_source_ref": record.source_ref,
        "_tgg_sgt": record.sgt,
        "_hermes_pa_job_type": "tgg_ops_ingest",
        "_hermes_pa_context": {
            "tenant": "tgg",
            "agent_id": "christopher",
            "job_type": "tgg_ops_ingest",
        },
    }
    return bridge


def _event_max_epoch_seconds(event: Any) -> int | None:
    """Latest message timestamp (epoch seconds) carried by a turn event.

    Handles both single-message events and debounce bundles
    ({"bundle": True, "messages": [...]}). bridge_message_log.ts is epoch
    seconds, and _record_to_bridge_message copies it into raw "timestamp".
    """
    raw = event.raw_message if isinstance(getattr(event, "raw_message", None), dict) else {}
    candidates = raw.get("messages") if raw.get("bundle") else [raw]
    if not isinstance(candidates, list):
        candidates = [raw]
    best: int | None = None
    for message in candidates:
        if not isinstance(message, dict):
            continue
        ts = message.get("timestamp")
        if isinstance(ts, dict):
            ts = ts.get("low") or ts.get("value")
        try:
            ts_num = int(float(ts))
        except (TypeError, ValueError):
            continue
        if ts_num > 0:
            best = ts_num if best is None else max(best, ts_num)
    return best


def _extract_latest_assistant(messages: list[dict[str, Any]], start: int = 0) -> str:
    for msg in reversed(messages[start:]):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg.get("content"))
    return ""


def _extract_tool_names(messages: list[dict[str, Any]], start: int = 0) -> list[str]:
    out: list[str] = []
    for msg in messages[start:]:
        name = msg.get("tool_name")
        if name:
            out.append(str(name))
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if isinstance(function, dict) and function.get("name"):
                out.append(str(function["name"]))
    seen = set()
    deduped = []
    for name in out:
        if name not in seen:
            deduped.append(name)
            seen.add(name)
    return deduped


def _message_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not name:
            continue
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {"_raw": raw_args}
        out.append(
            {
                "id": call.get("id") or call.get("call_id"),
                "name": str(name),
                "arguments": args if isinstance(args, dict) else {"value": args},
            }
        )
    return out


def _tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            for call in _message_tool_calls(msg):
                call_id = str(call.get("id") or "")
                if call_id:
                    calls[call_id] = call
                ordered.append({"call": call, "result": None})
        elif msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            call = calls.get(call_id, {"id": call_id, "name": msg.get("name") or "tool", "arguments": {}})
            content = msg.get("content") or ""
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
            except Exception:
                parsed = {"_raw": content}
            paired = False
            for item in reversed(ordered):
                if item.get("result") is None and item.get("call", {}).get("id") == call_id:
                    item["result"] = parsed
                    paired = True
                    break
            if not paired:
                ordered.append({"call": call, "result": parsed})
    return ordered


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _tool_result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        payload = result.get("payload")
        if isinstance(payload, dict):
            return payload
        data = result.get("data")
        if isinstance(data, dict):
            return data
    return {}


def _lookup_candidates(pair: dict[str, Any]) -> list[dict[str, Any]]:
    call = pair.get("call") or {}
    name = str(call.get("name") or "")
    if name not in {"tgg_case_lookup", "tgg_case_search", "case_lookup", "case_search"}:
        return []
    result = pair.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "job_no": _first_text(row.get("jobNo"), row.get("job_no"), row.get("normalized_job_no")),
                "address": _first_text(row.get("address"), row.get("unitAddress"), row.get("unit_address")),
                "match_reasons": [
                    str(v)
                    for v in (
                        row.get("matchReasons")
                        or row.get("match_reasons")
                        or row.get("reasons")
                        or []
                    )
                    if v
                ],
            }
        )
    return out


def _status_effect_from_text(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("done", "complete", "completed", "install done", "replaced", "rectified")):
        return "reported_complete"
    if any(word in lower for word in ("new job", "job no", "assist", "please assist")):
        return "new_job_or_request"
    if any(word in lower for word in ("update", "arrange", "when", "follow up", "follow-up")):
        return "followup"
    return "observation"


def _case_effect_from_pair(pair: dict[str, Any], *, source_refs: list[str], assistant: str) -> dict[str, Any] | None:
    call = pair.get("call") or {}
    name = str(call.get("name") or "")
    if name not in {"tgg_case_observation", "case_observation", "tgg_case_create", "case_create"}:
        return None
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    result = pair.get("result")
    payload = _tool_result_payload(result)
    job_no = _first_text(
        payload.get("jobNo"),
        payload.get("job_no"),
        args.get("jobNo"),
        args.get("job_no"),
        args.get("case_id"),
    )
    notes = _first_text(payload.get("notes"), args.get("notes"), args.get("messageText"), args.get("message_text"), assistant)
    effect = _status_effect_from_text(notes or assistant or "")
    if "create" in name:
        effect = "new_case_dry_run"
    confidence = _first_text(payload.get("confidence"), args.get("confidence")) or ("high" if job_no else "low")
    case_match = "tool_lookup" if job_no else "unmatched"
    return {
        "status_effect": effect,
        "confidence": confidence,
        "case_match": case_match,
        "normalized_job_no": job_no,
        "summary": notes or assistant or "",
        "evidence_source_refs": source_refs,
        "needs_human_confirmation": not bool(job_no) or "create" in name,
        "reason": _first_text(
            payload.get("reason"),
            result.get("message") if isinstance(result, dict) else None,
            "dry-run tool call captured from Hermes replay",
        ),
    }


def _action_from_pair(pair: dict[str, Any], *, assistant: str) -> dict[str, Any] | None:
    call = pair.get("call") or {}
    name = str(call.get("name") or "")
    if name not in {"tgg_case_observation", "case_observation", "tgg_case_create", "case_create"}:
        return None
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    result = pair.get("result")
    payload = _tool_result_payload(result)
    return {
        "action_type": "create_case_dry_run" if "create" in name else "record_observation",
        "should_send": False,
        "needs_human_approval": False,
        "draft_message": None,
        "reason": _first_text(
            payload.get("reason"),
            result.get("message") if isinstance(result, dict) else None,
            args.get("notes"),
            assistant,
        ),
    }


def _pending_questions(assistant: str) -> list[dict[str, Any]]:
    lower = assistant.lower()
    if "confirm" not in lower and "which job" not in lower and "can you" not in lower:
        return []
    return [{"question": assistant.strip()}] if assistant.strip() else []


def _source_refs_from_event(event: Any) -> list[str]:
    raw = event.raw_message if isinstance(event.raw_message, dict) else {}
    refs: list[str] = []
    messages = raw.get("messages") if raw.get("bundle") else [raw]
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        ref = message.get("_tgg_source_ref") or message.get("source_ref")
        if ref:
            refs.append(str(ref))
    if not refs:
        for value in raw.get("sourceMessageIds") or []:
            if value:
                refs.append(str(value))
    if not refs and getattr(event, "message_id", None):
        refs.append(str(event.message_id))
    return refs


def _build_review_result(
    *,
    turn: PublishedTurn,
) -> dict[str, Any]:
    pairs = _tool_pairs(turn.segment)
    lookup = []
    case_effects = []
    actions = []
    for pair in pairs:
        lookup.extend(_lookup_candidates(pair))
        effect = _case_effect_from_pair(pair, source_refs=turn.source_refs, assistant=turn.assistant)
        if effect:
            case_effects.append(effect)
        action = _action_from_pair(pair, assistant=turn.assistant)
        if action:
            actions.append(action)
    return {
        "run_id": None,
        "turn_id": turn.turn_id,
        "processor_version": "hermes-gateway-replay-v1",
        "provider": turn.provider or "openai-direct-primary",
        "model": turn.model or "gpt-5.4-mini",
        "status": "ok",
        "turn_summary": turn.assistant,
        "case_effects_json": json.dumps(case_effects, ensure_ascii=False),
        "actions_json": json.dumps(actions, ensure_ascii=False),
        "pending_questions_json": json.dumps(_pending_questions(turn.assistant), ensure_ascii=False),
        "lookup_json": json.dumps(lookup, ensure_ascii=False),
        "model_input_json": json.dumps(
            {
                "source_refs": turn.source_refs,
                "input_tokens": turn.input_tokens,
                "cached_input_tokens": turn.cached_input_tokens,
                "output_tokens": turn.output_tokens,
                "reasoning_output_tokens": turn.reasoning_output_tokens,
                "estimated_cost_usd": turn.estimated_cost_usd,
                "llm_call_count": turn.llm_call_count,
                "llm_calls": turn.llm_calls,
            },
            ensure_ascii=False,
        ),
        "model_output_json": json.dumps(
            {
                "assistant": turn.assistant,
                "tools": pairs,
            },
            ensure_ascii=False,
        ),
        "error": None,
    }


def _result_usage(messages: list[dict[str, Any]], start: int = 0) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    for msg in messages[start:]:
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return input_tokens, output_tokens


def _capture_response_usage(files: list[Path]) -> dict[str, int]:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload = data.get("payload") if isinstance(data, dict) else {}
        raw_usage = payload.get("usage") if isinstance(payload, dict) else {}
        if not isinstance(raw_usage, dict):
            continue
        usage["input_tokens"] += _as_int(raw_usage.get("input_tokens") or raw_usage.get("prompt_tokens"))
        usage["cached_input_tokens"] += _as_int(
            (raw_usage.get("input_tokens_details") or {}).get("cached_tokens")
        )
        usage["output_tokens"] += _as_int(raw_usage.get("output_tokens") or raw_usage.get("completion_tokens"))
        usage["reasoning_output_tokens"] += _as_int(
            (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens")
        )
    return usage


def _pricing_provider(provider: str | None) -> str | None:
    raw = (provider or "").strip().lower()
    if not raw or raw in {"main", "openai-direct-primary"}:
        return "openai"
    if raw.startswith("openai"):
        return "openai"
    if raw.startswith("gemini") or raw.startswith("google"):
        return "gemini"
    return raw


def _pricing_model(model: str) -> str:
    return re.sub(r"-20\d\d-\d\d-\d\d$", "", str(model or "").strip())


def _estimate_cost_for_usage(
    *,
    model: str,
    provider: str | None,
    input_total: int,
    cached_input: int,
    output_tokens: int,
) -> float:
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

        cost = estimate_usage_cost(
            _pricing_model(model),
            CanonicalUsage(
                input_tokens=max(0, input_total - cached_input),
                cache_read_tokens=cached_input,
                output_tokens=output_tokens,
            ),
            provider=_pricing_provider(provider),
        )
        return float(cost.amount_usd or 0.0)
    except Exception:
        return 0.0


def _capture_response_calls(
    files: list[Path],
    *,
    model: str,
    provider: str | None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for idx, path in enumerate(files, start=1):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload = data.get("payload") if isinstance(data, dict) else {}
        if not isinstance(payload, dict):
            continue
        raw_usage = payload.get("usage") if isinstance(payload, dict) else {}
        if not isinstance(raw_usage, dict):
            continue
        input_total = _as_int(raw_usage.get("input_tokens") or raw_usage.get("prompt_tokens"))
        cached_input = _as_int((raw_usage.get("input_tokens_details") or {}).get("cached_tokens"))
        output_tokens = _as_int(raw_usage.get("output_tokens") or raw_usage.get("completion_tokens"))
        reasoning_tokens = _as_int((raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens"))
        call_model = str(payload.get("model") or data.get("model") or model)
        call_provider = str(payload.get("provider") or data.get("provider") or provider or "openai-direct-primary")
        calls.append(
            {
                "index": idx,
                "capture_file": str(path),
                "model": call_model,
                "provider": call_provider,
                "input_tokens": input_total,
                "cached_input_tokens": cached_input,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_tokens,
                "estimated_cost_usd": _estimate_cost_for_usage(
                    model=call_model,
                    provider=call_provider,
                    input_total=input_total,
                    cached_input=cached_input,
                    output_tokens=output_tokens,
                ),
            }
        )
    return calls


def _capture_call_ledger(path: Path | None, start_offset: int) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    calls: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(start_offset)
        for idx, line in enumerate(fh, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            calls.append(
                {
                    "index": idx,
                    "capture_file": str(path),
                    "task": row.get("task"),
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "input_tokens": _as_int(row.get("input_tokens")),
                    "cached_input_tokens": _as_int(row.get("cached_input_tokens")),
                    "output_tokens": _as_int(row.get("output_tokens")),
                    "reasoning_output_tokens": _as_int(row.get("reasoning_tokens")),
                    "estimated_cost_usd": _as_number(row.get("cost_usd")),
                    "cost_status": row.get("cost_status"),
                    "latency_ms": _as_int(row.get("latency_ms")),
                    "turn_id": row.get("turn_id"),
                }
            )
    return calls


def _summarize_llm_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "llm_call_count": len(calls),
        "input_tokens": sum(_as_int(call.get("input_tokens")) for call in calls),
        "cached_input_tokens": sum(_as_int(call.get("cached_input_tokens")) for call in calls),
        "output_tokens": sum(_as_int(call.get("output_tokens")) for call in calls),
        "reasoning_output_tokens": sum(_as_int(call.get("reasoning_output_tokens")) for call in calls),
        "estimated_cost_usd": sum(_as_number(call.get("estimated_cost_usd")) for call in calls),
    }


def _publish_review_run(
    *,
    db_path: Path,
    run_id: str,
    records: list[ReplayRecord],
    turn_results: list[dict[str, Any]],
    run_label: str,
    model: str,
    debounce_seconds: int,
    turn_offset: int = 0,
) -> dict[str, Any]:
    if not records:
        raise RuntimeError("Cannot publish empty replay run")
    now_ts = int(datetime.now().timestamp())
    chat_scope = sorted({record.chat_jid for record in records})
    turn_policy = {
        chat_id: {
            "process_all": True,
            "debounce_seconds": debounce_seconds,
            "direct_mention_immediate": True,
        }
        for chat_id in chat_scope
    }
    published_turns: list[PublishedTurn] = []
    for index, result in enumerate(turn_results, start=turn_offset + 1):
        event = result["event"]
        source_refs = _source_refs_from_event(event)
        if not source_refs:
            source_refs = [f"turn-{index}"]
        start_record = next((record for record in records if record.source_ref == source_refs[0]), None)
        end_record = next((record for record in reversed(records) if record.source_ref == source_refs[-1]), None)
        turn_id = f"{run_id}:turn:{index:04d}"
        published_turns.append(
            PublishedTurn(
                turn_id=turn_id,
                event=event,
                segment=result.get("segment") or [],
                source_refs=source_refs,
                session_id=str(result.get("session_id") or ""),
                input_tokens=_as_int(result.get("input_tokens")),
                cached_input_tokens=_as_int(result.get("cached_input_tokens")),
                output_tokens=_as_int(result.get("output_tokens")),
                reasoning_output_tokens=_as_int(result.get("reasoning_output_tokens")),
                estimated_cost_usd=_as_number(result.get("estimated_cost_usd")),
                llm_call_count=_as_int(result.get("llm_call_count")),
                llm_calls=result.get("llm_calls") if isinstance(result.get("llm_calls"), list) else [],
                model=str(result.get("model") or model),
                provider=str(result.get("provider") or "openai-direct-primary"),
                assistant=str(result.get("assistant") or ""),
            )
        )

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO tgg_christopher_runs
                  (run_id, mode, clock_mode, status, source_adapter, chat_scope_json,
                   replay_window_start_ts, replay_window_end_ts, debounce_enabled,
                   quiet_window_seconds, direct_mention_immediate, detect_only,
                   turn_policy_json, metadata_json, created_at, started_at, ended_at, updated_at)
                VALUES (?, 'replay', 'virtual', 'settled', 'hermes-whatsapp-replay',
                        ?, ?, ?, 1, ?, 1, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status = excluded.status,
                  chat_scope_json = excluded.chat_scope_json,
                  replay_window_start_ts = MIN(COALESCE(tgg_christopher_runs.replay_window_start_ts, excluded.replay_window_start_ts), excluded.replay_window_start_ts),
                  replay_window_end_ts = MAX(COALESCE(tgg_christopher_runs.replay_window_end_ts, excluded.replay_window_end_ts), excluded.replay_window_end_ts),
                  quiet_window_seconds = excluded.quiet_window_seconds,
                  turn_policy_json = excluded.turn_policy_json,
                  metadata_json = excluded.metadata_json,
                  started_at = MIN(COALESCE(tgg_christopher_runs.started_at, excluded.started_at), excluded.started_at),
                  ended_at = MAX(COALESCE(tgg_christopher_runs.ended_at, excluded.ended_at), excluded.ended_at),
                  updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    json.dumps(chat_scope),
                    records[0].ts,
                    records[-1].ts,
                    debounce_seconds,
                    json.dumps(turn_policy),
                    json.dumps(
                        {
                            "label": run_label,
                            "publisher": "tgg_christopher_hermes_replay.py",
                            "processor_version": "hermes-gateway-replay-v1",
                            "model": model,
                        },
                        ensure_ascii=False,
                    ),
                    now_ts,
                    records[0].ts,
                    records[-1].ts,
                    now_ts,
                ),
            )
            queue_ids: dict[str, int] = {}
            for record in records:
                cursor = conn.execute(
                    """
                    INSERT INTO tgg_christopher_message_queue
                      (run_id, mode, chat_id, chat_name, source_ref, source_kind,
                       sender_id, sender_label, sender_role_guess, from_me, ts, sgt,
                       text, message_kind, has_media, media_refs_json, reply_to_source_ref,
                       quoted_text, mentioned_ids_json, raw_json, state, turn_id, error,
                       ingested_at, updated_at)
                    VALUES (?, 'replay', ?, ?, ?, 'hermes_replay', ?, ?, NULL, 0, ?, ?,
                            ?, ?, ?, ?, ?, ?, '[]', ?, 'queued', NULL, NULL, ?, ?)
                    ON CONFLICT(run_id, source_ref) DO UPDATE SET
                      chat_id = excluded.chat_id,
                      chat_name = excluded.chat_name,
                      sender_id = excluded.sender_id,
                      sender_label = excluded.sender_label,
                      ts = excluded.ts,
                      sgt = excluded.sgt,
                      text = excluded.text,
                      message_kind = excluded.message_kind,
                      has_media = excluded.has_media,
                      media_refs_json = excluded.media_refs_json,
                      reply_to_source_ref = excluded.reply_to_source_ref,
                      quoted_text = excluded.quoted_text,
                      raw_json = excluded.raw_json,
                      updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        record.chat_jid,
                        record.chat_name,
                        record.source_ref,
                        record.sender_id or None,
                        record.sender_id or None,
                        record.ts,
                        record.sgt,
                        record.text,
                        record.message_kind or "text",
                        1 if record.has_media else 0,
                        json.dumps(record.media_refs, ensure_ascii=False),
                        record.reply_to_source_ref or None,
                        record.quoted_text or None,
                        json.dumps(record.raw_json, ensure_ascii=False),
                        now_ts,
                        now_ts,
                    ),
                )
                if int(cursor.lastrowid or 0):
                    queue_ids[record.source_ref] = int(cursor.lastrowid)
                else:
                    row = conn.execute(
                        """
                        SELECT id FROM tgg_christopher_message_queue
                        WHERE run_id = ? AND source_ref = ?
                        """,
                        (run_id, record.source_ref),
                    ).fetchone()
                    if row:
                        queue_ids[record.source_ref] = int(row[0])

            record_by_ref = {record.source_ref: record for record in records}
            for turn in published_turns:
                turn_records = [record_by_ref[ref] for ref in turn.source_refs if ref in record_by_ref]
                if not turn_records:
                    continue
                conn.execute(
                    """
                    INSERT INTO tgg_christopher_turns
                      (turn_id, run_id, chat_id, chat_name, turn_start_ts, turn_end_ts,
                       turn_start_sgt, turn_end_sgt, message_count, media_count,
                       closed_reason, debounce_enabled, quiet_window_seconds,
                       direct_mention, policy_json, summary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quiet_window', 1, ?, 0, ?, ?, ?)
                    ON CONFLICT(turn_id) DO UPDATE SET
                      chat_id = excluded.chat_id,
                      chat_name = excluded.chat_name,
                      turn_start_ts = excluded.turn_start_ts,
                      turn_end_ts = excluded.turn_end_ts,
                      turn_start_sgt = excluded.turn_start_sgt,
                      turn_end_sgt = excluded.turn_end_sgt,
                      message_count = excluded.message_count,
                      media_count = excluded.media_count,
                      closed_reason = excluded.closed_reason,
                      debounce_enabled = excluded.debounce_enabled,
                      quiet_window_seconds = excluded.quiet_window_seconds,
                      direct_mention = excluded.direct_mention,
                      policy_json = excluded.policy_json,
                      summary_json = excluded.summary_json
                    """,
                    (
                        turn.turn_id,
                        run_id,
                        turn_records[0].chat_jid,
                        turn_records[0].chat_name,
                        turn_records[0].ts,
                        turn_records[-1].ts,
                        turn_records[0].sgt,
                        turn_records[-1].sgt,
                        len(turn_records),
                        sum(1 for record in turn_records if record.has_media),
                        debounce_seconds,
                        json.dumps(turn_policy.get(turn_records[0].chat_jid, {})),
                        json.dumps(
                            {
                                "session_id": turn.session_id,
                                "input_tokens": turn.input_tokens,
                                "cached_input_tokens": turn.cached_input_tokens,
                                "output_tokens": turn.output_tokens,
                                "reasoning_output_tokens": turn.reasoning_output_tokens,
                                "estimated_cost_usd": turn.estimated_cost_usd,
                                "llm_call_count": turn.llm_call_count,
                            },
                            ensure_ascii=False,
                        ),
                        now_ts,
                    ),
                )
                for record in turn_records:
                    queue_id = queue_ids.get(record.source_ref)
                    if not queue_id:
                        continue
                    conn.execute(
                        """
                        DELETE FROM tgg_christopher_turn_messages
                        WHERE run_id = ? AND turn_id = ? AND source_ref = ?
                        """,
                        (run_id, turn.turn_id, record.source_ref),
                    )
                    conn.execute(
                        """
                        INSERT INTO tgg_christopher_turn_messages
                          (run_id, turn_id, queue_id, source_ref, ts)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        """,
                        (run_id, turn.turn_id, queue_id, record.source_ref, record.ts),
                    )
                    conn.execute(
                        """
                        UPDATE tgg_christopher_message_queue
                        SET state = 'turn_processed', turn_id = ?, updated_at = ?
                        WHERE run_id = ? AND id = ?
                        """,
                        (turn.turn_id, now_ts, run_id, queue_id),
                    )
                review = _build_review_result(turn=turn)
                review["run_id"] = run_id
                conn.execute(
                    """
                    INSERT INTO tgg_christopher_turn_results
                      (run_id, turn_id, processor_version, provider, model, status,
                       turn_summary, case_effects_json, actions_json,
                       pending_questions_json, lookup_json, model_input_json,
                       model_output_json, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, turn_id, processor_version) DO UPDATE SET
                      provider = excluded.provider,
                      model = excluded.model,
                      status = excluded.status,
                      turn_summary = excluded.turn_summary,
                      case_effects_json = excluded.case_effects_json,
                      actions_json = excluded.actions_json,
                      pending_questions_json = excluded.pending_questions_json,
                      lookup_json = excluded.lookup_json,
                      model_input_json = excluded.model_input_json,
                      model_output_json = excluded.model_output_json,
                      error = excluded.error,
                      updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        turn.turn_id,
                        review["processor_version"],
                        review["provider"],
                        review["model"],
                        review["status"],
                        review["turn_summary"],
                        review["case_effects_json"],
                        review["actions_json"],
                        review["pending_questions_json"],
                        review["lookup_json"],
                        review["model_input_json"],
                        review["model_output_json"],
                        review["error"],
                        now_ts,
                        now_ts,
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        message_count = conn.execute(
            """
            SELECT COUNT(*) FROM tgg_christopher_message_queue
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        turn_count = conn.execute(
            """
            SELECT COUNT(*) FROM tgg_christopher_turns
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        result_count = conn.execute(
            """
            SELECT COUNT(*) FROM tgg_christopher_turn_results
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
    return {
        "run_id": run_id,
        "messages": int(message_count),
        "turns": int(turn_count),
        "results": int(result_count),
        "db": str(db_path),
    }


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int:
    return int(_as_number(value, 0.0))


def _llm_detail_html(calls: list[dict[str, Any]], fallback_count: int) -> str:
    if not calls:
        return f'<div class="llm-detail">main-model calls: {fallback_count or "unknown"} · per-call capture unavailable</div>'
    rows = []
    for call in calls:
        rows.append(
            f"""
            <tr>
              <td>{_as_int(call.get('index'))}</td>
              <td>{html.escape(str(call.get('model') or ''))}</td>
              <td>{_as_int(call.get('input_tokens')):,}</td>
              <td>{_as_int(call.get('cached_input_tokens')):,}</td>
              <td>{_as_int(call.get('output_tokens')):,}</td>
              <td>{_as_int(call.get('reasoning_output_tokens')):,}</td>
              <td>${_as_number(call.get('estimated_cost_usd')):.6f}</td>
            </tr>
            """
        )
    return f"""
    <details class="llm-detail">
      <summary>{len(calls)} main-model call(s) · ${sum(_as_number(call.get('estimated_cost_usd')) for call in calls):.6f}</summary>
      <table>
        <thead><tr><th>#</th><th>model</th><th>in</th><th>cached</th><th>out</th><th>reasoning</th><th>cost</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </details>
    """


def _html_report(
    *,
    output_path: Path,
    run_label: str,
    model: str,
    db_path: Path,
    records: list[ReplayRecord],
    turn_results: list[dict[str, Any]],
    hermes_home: Path,
    session_id: str,
) -> None:
    rows = []
    for index, result in enumerate(turn_results, start=1):
        event = result["event"]
        text = str(event.text or "")
        source_ids = []
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        if raw.get("bundle"):
            source_ids = [str(v) for v in raw.get("sourceMessageIds") or []]
        elif event.message_id:
            source_ids = [str(event.message_id)]
        rows.append(
            f"""
            <section class="turn">
              <div class="turn-head">
                <span>turn {index}</span>
                <span>{html.escape(str(event.message_id or ''))}</span>
              </div>
              <div class="meta">{len(source_ids)} source message(s) · tools: {html.escape(', '.join(result['tools']) or 'none')} · main-model calls: {_as_int(result.get('llm_call_count')) or 'unknown'} · {_as_int(result.get('input_tokens')):,} in ({_as_int(result.get('cached_input_tokens')):,} cached) / {_as_int(result.get('output_tokens')):,} out ({_as_int(result.get('reasoning_output_tokens')):,} reasoning) · ${_as_number(result.get('estimated_cost_usd')):.6f}</div>
              {_llm_detail_html(result.get('llm_calls') if isinstance(result.get('llm_calls'), list) else [], _as_int(result.get('llm_call_count')))}
              <pre class="inbound">{html.escape(text)}</pre>
              <pre class="assistant">{html.escape(result['assistant'] or '[no assistant transcript row]')}</pre>
            </section>
            """
        )
    first = records[0].sgt if records else "n/a"
    last = records[-1].sgt if records else "n/a"
    total_in = sum(_as_int(r.get("input_tokens")) for r in turn_results)
    total_cached = sum(_as_int(r.get("cached_input_tokens")) for r in turn_results)
    total_out = sum(_as_int(r.get("output_tokens")) for r in turn_results)
    total_reasoning = sum(_as_int(r.get("reasoning_output_tokens")) for r in turn_results)
    total_cost = sum(_as_number(r.get("estimated_cost_usd")) for r in turn_results)
    total_llm_calls = sum(_as_int(r.get("llm_call_count")) for r in turn_results)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{html.escape(run_label)}</title>
  <style>
    body {{ margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f4ef; color: #171717; }}
    header {{ padding: 28px 32px; background: #123c42; color: white; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; padding: 20px 32px; background: white; border-bottom: 1px solid #dedbd2; }}
    .summary div {{ padding: 12px; background: #f2f7f6; border: 1px solid #d7e6e3; border-radius: 8px; }}
    .summary b {{ display: block; font-size: 18px; }}
    main {{ padding: 24px 32px 40px; max-width: 1180px; margin: 0 auto; }}
    .turn {{ background: white; border: 1px solid #dedbd2; border-radius: 8px; margin-bottom: 18px; overflow: hidden; }}
    .turn-head {{ display: flex; justify-content: space-between; gap: 16px; background: #e6eee9; padding: 10px 14px; font-weight: 700; }}
    .meta {{ padding: 8px 14px; color: #555; border-bottom: 1px solid #eee9df; }}
    .llm-detail {{ padding: 8px 14px; color: #555; border-bottom: 1px solid #eee9df; }}
    .llm-detail summary {{ cursor: pointer; color: #171717; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }}
    th, td {{ text-align: left; padding: 5px 6px; border-bottom: 1px solid #eee9df; }}
    pre {{ margin: 0; padding: 14px; white-space: pre-wrap; word-break: break-word; font: 13px/1.42 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .inbound {{ background: #fff; border-bottom: 1px solid #eee9df; }}
    .assistant {{ background: #f4f0fb; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(run_label)}</h1>
    <div>actual Hermes gateway replay · local copied-DB business writes · no prod mutation</div>
  </header>
  <section class="summary">
    <div><span>model</span><b>{html.escape(model)}</b></div>
    <div><span>messages</span><b>{len(records)}</b></div>
    <div><span>turns</span><b>{len(turn_results)}</b></div>
    <div><span>main calls</span><b>{total_llm_calls or 'unknown'}</b></div>
    <div><span>window</span><b>{html.escape(first)} → {html.escape(last)}</b></div>
    <div><span>tokens</span><b>{total_in:,} in / {total_out:,} out</b></div>
    <div><span>cache</span><b>{total_cached:,} cached in / {total_reasoning:,} reasoning out</b></div>
    <div><span>cost</span><b>${total_cost:.6f}</b></div>
    <div><span>session</span><b>{html.escape(session_id)}</b></div>
  </section>
  <main>
    <p><b>DB:</b> {html.escape(str(db_path))}<br><b>Hermes home:</b> {html.escape(str(hermes_home))}<br><b>Cost note:</b> totals cover captured main-model calls. Native vision pre-analysis calls are logged separately until the provider-agnostic call ledger lands.</p>
    {''.join(rows)}
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )


def _html_report_from_published_run(*, db_path: Path, run_id: str, output_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            """
            SELECT * FROM tgg_christopher_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if not run:
            raise RuntimeError(f"No tgg_christopher_runs row for {run_id}")
        turns = conn.execute(
            """
            SELECT t.*, r.provider, r.model, r.status, r.turn_summary,
                   r.case_effects_json, r.actions_json, r.pending_questions_json,
                   r.lookup_json, r.model_input_json, r.model_output_json, r.error
            FROM tgg_christopher_turns t
            LEFT JOIN tgg_christopher_turn_results r
              ON r.run_id = t.run_id AND r.turn_id = t.turn_id
            WHERE t.run_id = ?
            ORDER BY t.turn_start_ts, t.turn_id
            """,
            (run_id,),
        ).fetchall()
        queue_rows = conn.execute(
            """
            SELECT * FROM tgg_christopher_message_queue
            WHERE run_id = ?
            ORDER BY ts, id
            """,
            (run_id,),
        ).fetchall()
        messages_by_turn: dict[str, list[sqlite3.Row]] = {}
        for row in queue_rows:
            turn_id = str(row["turn_id"] or "")
            if turn_id:
                messages_by_turn.setdefault(turn_id, []).append(row)

    def parse_json(raw: Any, fallback: Any) -> Any:
        if not raw:
            return fallback
        try:
            return json.loads(str(raw))
        except Exception:
            return fallback

    total_messages = len(queue_rows)
    total_media = sum(1 for row in queue_rows if int(row["has_media"] or 0))
    total_in = 0
    total_cached = 0
    total_out = 0
    total_reasoning = 0
    total_cost = 0.0
    total_llm_calls = 0
    session_ids: set[str] = set()
    row_html = []
    for index, turn in enumerate(turns, start=1):
        summary = parse_json(turn["summary_json"], {})
        model_input = parse_json(turn["model_input_json"], {})
        model_output = parse_json(turn["model_output_json"], {})
        input_tokens = _as_int(model_input.get("input_tokens") or summary.get("input_tokens"))
        cached_tokens = _as_int(model_input.get("cached_input_tokens") or summary.get("cached_input_tokens"))
        output_tokens = _as_int(model_input.get("output_tokens") or summary.get("output_tokens"))
        reasoning_tokens = _as_int(
            model_input.get("reasoning_output_tokens") or summary.get("reasoning_output_tokens")
        )
        cost = _as_number(model_input.get("estimated_cost_usd") or summary.get("estimated_cost_usd"))
        llm_calls = model_input.get("llm_calls") if isinstance(model_input.get("llm_calls"), list) else []
        llm_call_count = _as_int(model_input.get("llm_call_count") or summary.get("llm_call_count") or len(llm_calls))
        total_in += input_tokens
        total_cached += cached_tokens
        total_out += output_tokens
        total_reasoning += reasoning_tokens
        total_cost += cost
        total_llm_calls += llm_call_count
        session_id = str(summary.get("session_id") or "")
        if session_id:
            session_ids.add(session_id)
        messages = messages_by_turn.get(str(turn["turn_id"]), [])
        message_blocks = []
        for message in messages:
            media_refs = parse_json(message["media_refs_json"], [])
            media_label = f" · media {len(media_refs)}" if media_refs else ""
            text = str(message["text"] or "")
            message_blocks.append(
                f"""
                <div class="message">
                  <div class="msg-meta">{html.escape(str(message['sgt']))} · {html.escape(str(message['sender_label'] or message['sender_id'] or 'unknown'))}{html.escape(media_label)}</div>
                  <pre>{html.escape(text or '[media only]')}</pre>
                </div>
                """
            )
        assistant = str(model_output.get("assistant") or turn["turn_summary"] or "[no assistant output]")
        tools = model_output.get("tools") if isinstance(model_output.get("tools"), list) else []
        tool_names = []
        for pair in tools:
            call = pair.get("call") if isinstance(pair, dict) else None
            name = call.get("name") if isinstance(call, dict) else None
            if name:
                tool_names.append(str(name))
        effects = parse_json(turn["case_effects_json"], [])
        actions = parse_json(turn["actions_json"], [])
        questions = parse_json(turn["pending_questions_json"], [])
        row_html.append(
            f"""
            <section class="turn">
              <div class="turn-head">
                <div>
                  <span class="turn-num">turn {index}</span>
                  <b>{html.escape(str(turn['turn_start_sgt']))} → {html.escape(str(turn['turn_end_sgt']))}</b>
                </div>
                <div>{int(turn['message_count'] or 0)} msg · {int(turn['media_count'] or 0)} media</div>
              </div>
              <div class="meta">
                tools: {html.escape(', '.join(tool_names) or 'none')} ·
                main-model calls: {llm_call_count or 'unknown'} ·
                {input_tokens:,} in ({cached_tokens:,} cached) / {output_tokens:,} out ({reasoning_tokens:,} reasoning) ·
                {'$' + format(cost, '.6f') if cost else 'cost not computed'}
              </div>
              {_llm_detail_html(llm_calls, llm_call_count)}
              <div class="cols">
                <div>
                  <h2>WhatsApp input</h2>
                  {''.join(message_blocks) or '<p class="empty">no source messages attached</p>'}
                </div>
                <div>
                  <h2>Christopher read</h2>
                  <pre class="assistant">{html.escape(assistant)}</pre>
                  <div class="chips">
                    <span>{len(effects)} case effect(s)</span>
                    <span>{len(actions)} action(s)</span>
                    <span>{len(questions)} pending question(s)</span>
                  </div>
                </div>
              </div>
            </section>
            """
        )

    first = str(queue_rows[0]["sgt"]) if queue_rows else "n/a"
    last = str(queue_rows[-1]["sgt"]) if queue_rows else "n/a"
    run_meta = parse_json(run["metadata_json"], {})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{html.escape(run_id)} · Christopher replay review</title>
  <style>
    :root {{ color-scheme: light; --ink: #18211f; --muted: #66716d; --line: #d9ded8; --paper: #f5f2eb; --panel: #fffdf8; --wa: #0b7a67; --read: #f2edf9; --read-line: #d6c9ee; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--paper); color: var(--ink); }}
    header {{ padding: 28px 32px; background: var(--wa); color: white; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 10px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; padding: 18px 32px; background: white; border-bottom: 1px solid var(--line); }}
    .summary div {{ padding: 10px 12px; background: #f3f8f6; border: 1px solid #d6e8e3; border-radius: 8px; }}
    .summary span {{ display: block; color: var(--muted); font-size: 12px; }}
    .summary b {{ display: block; margin-top: 4px; font-size: 17px; }}
    main {{ padding: 24px 32px 44px; max-width: 1320px; margin: 0 auto; }}
    .note {{ margin: 0 0 18px; color: var(--muted); }}
    .turn {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 18px; overflow: hidden; }}
    .turn-head {{ display: flex; justify-content: space-between; gap: 16px; padding: 11px 14px; background: #e7f1ee; border-bottom: 1px solid var(--line); }}
    .turn-num {{ display: block; color: var(--muted); font-size: 12px; }}
    .meta {{ padding: 8px 14px; color: var(--muted); border-bottom: 1px solid #eee8dd; }}
    .llm-detail {{ padding: 8px 14px; color: var(--muted); border-bottom: 1px solid #eee8dd; }}
    .llm-detail summary {{ cursor: pointer; color: var(--ink); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }}
    th, td {{ text-align: left; padding: 5px 6px; border-bottom: 1px solid #eee8dd; }}
    .cols {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0; }}
    .cols > div {{ padding: 14px; }}
    .cols > div + div {{ border-left: 1px solid #eee8dd; background: var(--read); }}
    .message {{ background: white; border: 1px solid #e6e1d7; border-radius: 8px; margin-bottom: 10px; overflow: hidden; }}
    .msg-meta {{ padding: 7px 10px; background: #f8f7f3; color: var(--muted); font-size: 12px; border-bottom: 1px solid #eee8dd; }}
    pre {{ margin: 0; padding: 10px; white-space: pre-wrap; word-break: break-word; font: 13px/1.42 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .assistant {{ background: var(--read); border: 1px solid var(--read-line); border-radius: 8px; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .chips span {{ padding: 5px 8px; border-radius: 999px; background: white; border: 1px solid var(--read-line); color: #513e7c; font-size: 12px; }}
    .empty {{ color: var(--muted); margin: 0; }}
    @media (max-width: 820px) {{ .cols {{ grid-template-columns: 1fr; }} .cols > div + div {{ border-left: 0; border-top: 1px solid #eee8dd; }} header, .summary, main {{ padding-left: 16px; padding-right: 16px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Christopher replay review · MM2-SK</h1>
    <div>Hermes replay path · local copied-DB business writes · copied database only</div>
  </header>
  <section class="summary">
    <div><span>run</span><b>{html.escape(run_id)}</b></div>
    <div><span>model</span><b>{html.escape(str(run_meta.get('model') or 'gpt-5.4-mini'))}</b></div>
    <div><span>messages</span><b>{total_messages}</b></div>
    <div><span>turns</span><b>{len(turns)}</b></div>
    <div><span>main calls</span><b>{total_llm_calls or 'unknown'}</b></div>
    <div><span>media</span><b>{total_media}</b></div>
    <div><span>window</span><b>{html.escape(first)} → {html.escape(last)}</b></div>
    <div><span>session</span><b>{html.escape(', '.join(sorted(session_ids)) or 'n/a')}</b></div>
    <div><span>tokens</span><b>{total_in:,} in / {total_out:,} out</b></div>
    <div><span>cache</span><b>{total_cached:,} cached / {total_reasoning:,} reasoning</b></div>
    <div><span>cost</span><b>{'$' + format(total_cost, '.6f') if total_cost else 'not computed'}</b></div>
  </section>
  <main>
    <p class="note">Source DB: {html.escape(str(db_path))}. This is a local replay artifact; no production mutation. Cost totals cover captured main-model calls. Native vision pre-analysis calls are logged separately until the provider-agnostic call ledger lands.</p>
    {''.join(row_html)}
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "html": str(output_path),
        "messages": total_messages,
        "turns": len(turns),
        "media": total_media,
        "session_ids": sorted(session_ids),
        "input_tokens": total_in,
        "cached_input_tokens": total_cached,
        "output_tokens": total_out,
        "reasoning_output_tokens": total_reasoning,
        "estimated_cost_usd": total_cost,
        "llm_call_count": total_llm_calls,
    }


async def _run_nightly_compact(runner: Any, turn_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """End-of-day compaction step (--nightly-compact; v6.3 item 3, WB f6845320).

    AFTER the day's messages drain, fire a compaction on the chat's session via
    the SAME internal path the gateway's manual /compress command uses
    (GatewayRunner._handle_compress_command: builds a tmp_agent, applies the PA
    compression behavior, runs _compress_context, rewrites the transcript under
    the new session id, updates the session entry). Compression itself is NOT
    reimplemented here. The manual-compress path has no 200k autocompact
    threshold gate, so this runs even when the session is far below threshold —
    that is the point of a nightly: emulate the production 3am scheduled
    per-session compact so the NEXT day's --continue run resumes a compacted
    session instead of treadmilling into the threshold mid-day.
    """
    if not turn_results:
        print("[nightly-compact] skipped: no turns processed", file=sys.stderr, flush=True)
        return None
    from agent.model_metadata import estimate_messages_tokens_rough
    from gateway.platforms.base import MessageEvent

    last_event = turn_results[-1]["event"]
    source = last_event.source
    pre_entry = runner.session_store.get_or_create_session(source)
    pre_session_id = pre_entry.session_id
    pre_tokens = estimate_messages_tokens_rough(
        runner.session_store.load_transcript(pre_session_id) or []
    )
    event = MessageEvent(
        text="/compress",
        source=source,
        pa_job_type=getattr(last_event, "pa_job_type", None),
        pa_context=getattr(last_event, "pa_context", None),
    )
    gateway_reply = await runner._handle_compress_command(event)
    post_entry = runner.session_store.get_or_create_session(source)
    post_session_id = post_entry.session_id
    post_tokens = estimate_messages_tokens_rough(
        runner.session_store.load_transcript(post_session_id) or []
    )
    result = {
        "pre_session_id": pre_session_id,
        "post_session_id": post_session_id,
        "session_rotated": post_session_id != pre_session_id,
        "pre_estimated_tokens": pre_tokens,
        "post_estimated_tokens": post_tokens,
        "gateway_reply": str(gateway_reply or ""),
    }
    print(
        f"[nightly-compact] tokens ~{pre_tokens:,} -> ~{post_tokens:,}; "
        f"session {pre_session_id} -> {post_session_id}",
        file=sys.stderr,
        flush=True,
    )
    return result



def _stamp_replay_run(hermes_home: Path, *, run_label: str, chat_id: str,
                      since_sgt: str, until_sgt: str) -> int | None:
    """Record this invocation in the sandbox state DB (replay_runs).

    The replay-for window is a RUN INPUT (--since-sgt/--until-sgt) — recording
    it at run time is the deterministic source for "which day was this run
    for" (teren 2026-06-12: runs are named by the day we run FOR, not the day
    the run executed). Readers join pa_turns.started_at between
    started_wall/ended_wall — no content inference.
    """
    import sqlite3 as _sq
    state_db = hermes_home / "state.db"
    try:
        conn = _sq.connect(str(state_db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS replay_runs ("
            " id INTEGER PRIMARY KEY,"
            " run_label TEXT, chat_id TEXT,"
            " since_sgt TEXT, until_sgt TEXT,"
            " started_wall REAL, ended_wall REAL)"
        )
        cur = conn.execute(
            "INSERT INTO replay_runs (run_label, chat_id, since_sgt, until_sgt, started_wall)"
            " VALUES (?,?,?,?,?)",
            (run_label, chat_id, since_sgt, until_sgt, time.time()),
        )
        conn.commit()
        rid = cur.lastrowid
        conn.close()
        return rid
    except Exception as exc:  # stamp is best-effort; never blocks a run
        print(f"[replay-runs] stamp failed: {exc}", file=sys.stderr)
        return None


def _finalize_replay_run(hermes_home: Path, run_row_id: int | None) -> None:
    if run_row_id is None:
        return
    import sqlite3 as _sq
    try:
        conn = _sq.connect(str(hermes_home / "state.db"))
        conn.execute(
            "UPDATE replay_runs SET ended_wall=? WHERE id=?", (time.time(), run_row_id)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[replay-runs] finalize failed: {exc}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    profile = _resolve_replay_profile(args)
    _validate_provider_model_args(
        vision_provider=profile.vision_provider,
        vision_model=profile.vision_model,
    )
    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_label = args.run_label or f"tgg-hermes-replay-{args.chat_id.split('@', 1)[0]}-{run_stamp}"
    if args.continue_session:
        if not args.hermes_home:
            raise SystemExit(
                "--continue-session requires --hermes-home pointing at the prior "
                "run's hermes-home (a fresh tempdir has no session to resume)"
            )
        if args.cleanup_hermes_home:
            raise SystemExit(
                "--continue-session is incompatible with --cleanup-hermes-home "
                "(cleanup would delete the session store the next run resumes)"
            )
        if profile.rotate_session_every_turns:
            raise SystemExit(
                "--continue-session is incompatible with rotate_session_every_turns "
                "(rotation resets the session this mode exists to preserve)"
            )
    hermes_home = Path(args.hermes_home) if args.hermes_home else Path(tempfile.mkdtemp(prefix="tgg-hermes-replay-"))
    continue_plan = _continue_session_plan(
        hermes_home, continue_session=bool(args.continue_session)
    )
    if args.continue_session:
        print(f"[continue-session] {continue_plan['reason']}", file=sys.stderr)
    output_path = Path(args.output) if args.output else DOCS_DIR / f"{run_label}.html"

    _prepare_env(
        hermes_home,
        secrets=Path(args.secrets),
        live_business_writes=bool(args.live_business_writes),
    )
    _replay_run_row = _stamp_replay_run(
        hermes_home,
        run_label=run_label,
        chat_id=args.chat_id or "",
        since_sgt=args.since_sgt or "",
        until_sgt=args.until_sgt or "",
    )
    business_base_url = args.business_base_url
    local_backend: _ReplayOperatorBackend | None = None
    if not business_base_url and profile.business_mode == "copied-db-local-operator":
        local_backend = _ReplayOperatorBackend(Path(args.db))
        local_backend.start()
        atexit.register(local_backend.stop)
        business_base_url = local_backend.base_url
        # Business writes are safe here: the bridge points at the copied local DB.
        os.environ["HERMES_PA_BUSINESS_DRY_RUN"] = "0"
    elif business_base_url:
        # Eval-tenant backend (canonical since 2026-06-10, WB b7e19b21): the URL points
        # at the isolated eval tenant served by the REAL deployed systems app on
        # tgg-prod-sg (christopher-tgg-systems-eval.service, loopback :5192, separate
        # PS_DATA_DIR seeded from the baseline DB), reached via an ssh -L tunnel.
        # Real writes are enabled because _validate_replay_args refuses any
        # non-localhost --business-base-url (LOCAL_BUSINESS_PREFIXES + allow_prod_url),
        # so this can never target https://systems.papercut-labs.com directly.
        os.environ["HERMES_PA_BUSINESS_DRY_RUN"] = "0"
    _prepare_hermes_home(
        hermes_home,
        chat_id=args.chat_id,
        profile=profile,
        business_base_url=business_base_url,
        prod_pilot_run_id=args.prod_pilot_run_id,
        session_reset_mode=continue_plan["session_reset_mode"],
    )

    if profile.main_provider == "gemini":
        if not os.environ.get("GEMINI_API_KEY_PCL_PA_SHARED"):
            raise RuntimeError("GEMINI_API_KEY_PCL_PA_SHARED is not available in environment or secrets file")
    elif not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not available in environment or secrets file")
    if profile.vision_provider == "gemini" and not os.environ.get("GEMINI_API_KEY_PCL_PA_SHARED"):
        raise RuntimeError("GEMINI_API_KEY_PCL_PA_SHARED is not available in environment or secrets file")
    if not os.environ.get("CHRISTOPHER_TGG_PS_SERVICE_TOKEN"):
        raise RuntimeError("CHRISTOPHER_TGG_PS_SERVICE_TOKEN/BOBBY_TGG_PS_SERVICE_TOKEN is not available")

    # Import after HERMES_HOME is set; gateway/run reads it at module load.
    import gateway.run as gateway_run
    from gateway.config import Platform, load_gateway_config
    from gateway.platforms.whatsapp import WhatsAppAdapter
    from gateway.run import GatewayRunner

    records = _load_records(
        Path(args.db),
        chat_id=args.chat_id,
        since_sgt=args.since_sgt,
        until_sgt=args.until_sgt,
        limit=args.limit_messages,
        skip_messages=args.skip_messages,
        source_table=args.source_table,
    )
    if not records:
        raise RuntimeError(f"No {args.source_table} rows matched replay criteria")

    config = load_gateway_config()
    runner = GatewayRunner(config)
    runner._is_user_authorized = lambda source: True  # type: ignore[method-assign]

    adapter = WhatsAppAdapter(config.platforms[Platform.WHATSAPP])
    runner.adapters[Platform.WHATSAPP] = adapter

    turn_results: list[dict[str, Any]] = []
    captured_agent_actions: list[dict[str, Any]] = []
    original_record_pa_agent_action = gateway_run._record_pa_agent_action

    def capture_record_pa_agent_action(*args, **kwargs):
        captured_agent_actions.append(
            {
                "action_type": kwargs.get("action_type"),
                "status": kwargs.get("status"),
                "turn_id": kwargs.get("turn_id"),
                "cost_usd": kwargs.get("cost_usd", 0.0),
                "tokens_input": kwargs.get("tokens_input", 0),
                "tokens_output": kwargs.get("tokens_output", 0),
            }
        )
        return original_record_pa_agent_action(*args, **kwargs)

    gateway_run._record_pa_agent_action = capture_record_pa_agent_action

    handled_turns = 0

    async def handle_turn(event):
        nonlocal handled_turns
        source = event.source
        before = []
        session_id = ""
        capture_dir_raw = os.environ.get("HERMES_OPENAI_CAPTURE_DIR") or ""
        capture_dir = Path(capture_dir_raw) if capture_dir_raw else None
        response_captures_before = set(capture_dir.glob("*-response.json")) if capture_dir else set()
        ledger_raw = os.environ.get("HERMES_LLM_CALL_LOG") or ""
        ledger_path = Path(ledger_raw) if ledger_raw else None
        ledger_offset = ledger_path.stat().st_size if ledger_path and ledger_path.exists() else 0
        turn_env_value = f"{args.publish_review_run or run_label}:turn:{args.turn_offset + handled_turns + 1:04d}"
        previous_turn_env = os.environ.get("HERMES_LLM_TURN_ID")
        if source is not None:
            entry = runner.session_store.get_or_create_session(source)
            if profile.rotate_session_every_turns and handled_turns > 0 and handled_turns % profile.rotate_session_every_turns == 0:
                runner.session_store.reset_session(entry.session_key, display_name=source.chat_name or source.chat_id)
                runner._evict_cached_agent(entry.session_key)
                runner._release_running_agent_state(entry.session_key)
                entry = runner.session_store.get_or_create_session(source)
            session_id = entry.session_id
            before = runner.session_store.load_transcript(session_id)
        action_start = len(captured_agent_actions)
        os.environ["HERMES_LLM_TURN_ID"] = turn_env_value
        # Per-turn future cap: message_history_search must never see archive
        # rows from after the replayed moment. Cap = latest turn-message ts + 1
        # (exclusive `ts < cap` on the endpoint ⇒ everything up to and including
        # "now", nothing after). Live runtime never sets this env var.
        previous_before_env = os.environ.get("HERMES_PA_HISTORY_BEFORE_TS")
        turn_max_ts = _event_max_epoch_seconds(event)
        if turn_max_ts is not None:
            os.environ["HERMES_PA_HISTORY_BEFORE_TS"] = str(turn_max_ts + 1)
        try:
            returned = await runner._handle_message(event)
        finally:
            if previous_turn_env is None:
                os.environ.pop("HERMES_LLM_TURN_ID", None)
            else:
                os.environ["HERMES_LLM_TURN_ID"] = previous_turn_env
            if previous_before_env is None:
                os.environ.pop("HERMES_PA_HISTORY_BEFORE_TS", None)
            else:
                os.environ["HERMES_PA_HISTORY_BEFORE_TS"] = previous_before_env
        after = runner.session_store.load_transcript(session_id) if session_id else []
        segment = after[len(before):]
        assistant = _extract_latest_assistant(after, start=len(before))
        tools = _extract_tool_names(after, start=len(before))
        input_tokens, output_tokens = _result_usage(after, start=len(before))
        cached_input_tokens = 0
        reasoning_output_tokens = 0
        llm_calls: list[dict[str, Any]] = []
        llm_call_count = 0
        ledger_calls = _capture_call_ledger(ledger_path, ledger_offset)
        if ledger_calls:
            llm_calls = ledger_calls
            capture_usage = _summarize_llm_calls(llm_calls)
            input_tokens = capture_usage["input_tokens"]
            cached_input_tokens = capture_usage["cached_input_tokens"]
            output_tokens = capture_usage["output_tokens"]
            reasoning_output_tokens = capture_usage["reasoning_output_tokens"]
            llm_call_count = _as_int(capture_usage.get("llm_call_count"))
        elif capture_dir:
            response_captures_after = set(capture_dir.glob("*-response.json"))
            new_response_captures = sorted(response_captures_after - response_captures_before)
            llm_calls = _capture_response_calls(
                new_response_captures,
                model=profile.model,
                provider=profile.main_provider,
            )
            capture_usage = _summarize_llm_calls(llm_calls)
            if capture_usage["input_tokens"] or capture_usage["output_tokens"]:
                input_tokens = capture_usage["input_tokens"]
                cached_input_tokens = capture_usage["cached_input_tokens"]
                output_tokens = capture_usage["output_tokens"]
                reasoning_output_tokens = capture_usage["reasoning_output_tokens"]
            llm_call_count = _as_int(capture_usage.get("llm_call_count"))
        estimated_cost_usd = 0.0
        result_model = None
        result_provider = None
        if isinstance(returned, dict):
            input_tokens = _as_int(returned.get("input_tokens") or returned.get("prompt_tokens") or input_tokens)
            output_tokens = _as_int(returned.get("output_tokens") or returned.get("completion_tokens") or output_tokens)
            estimated_cost_usd = _as_number(returned.get("estimated_cost_usd"))
            result_model = returned.get("model")
            result_provider = returned.get("provider")
            llm_call_count = max(llm_call_count, _as_int(returned.get("api_calls")))
        if not estimated_cost_usd and llm_calls:
            estimated_cost_usd = _as_number(capture_usage.get("estimated_cost_usd"))
        if not input_tokens and not output_tokens:
            for action in reversed(captured_agent_actions[action_start:]):
                if action.get("action_type") != "dry-run-reply":
                    continue
                input_tokens = _as_int(action.get("tokens_input"))
                output_tokens = _as_int(action.get("tokens_output"))
                estimated_cost_usd = _as_number(action.get("cost_usd"))
                break
        if not llm_call_count and (input_tokens or output_tokens):
            llm_call_count = 1
        turn_results.append(
            {
                "event": event,
                "returned": returned,
                "segment": segment,
                "assistant": assistant,
                "tools": tools,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "llm_call_count": llm_call_count,
                "llm_calls": llm_calls,
                "model": result_model,
                "provider": result_provider,
                "session_id": session_id,
            }
        )
        handled_turns += 1
        if args.publish_review_run:
            _publish_review_run(
                db_path=Path(args.publish_review_db or args.db),
                run_id=args.publish_review_run,
                records=records,
                turn_results=turn_results,
                run_label=run_label,
                model=profile.model,
                debounce_seconds=profile.debounce_seconds,
                turn_offset=args.turn_offset,
            )

    adapter.handle_message = handle_turn  # type: ignore[method-assign]
    # Reaction skip: bare reaction messages never trigger replay turns.
    feed_records = [record for record in records if not _is_bare_reaction_record(record)]
    skipped_reactions = len(records) - len(feed_records)
    if skipped_reactions:
        print(f"skipped {skipped_reactions} bare reaction message(s) at the replay feed", file=sys.stderr)
    messages = [_record_to_bridge_message(record) for record in feed_records]
    try:
        processed = await adapter.replay_bridge_messages(messages)
    finally:
        gateway_run._record_pa_agent_action = original_record_pa_agent_action
    if processed != len(feed_records):
        print(f"processed {processed}/{len(feed_records)} bridge rows", file=sys.stderr)

    nightly_compact_result = None
    if args.nightly_compact:
        nightly_compact_result = await _run_nightly_compact(runner, turn_results)

    session_ids = sorted({str(r.get("session_id") or "") for r in turn_results if r.get("session_id")})
    session_id = session_ids[-1] if session_ids else ""
    _html_report(
        output_path=output_path,
        run_label=run_label,
        model=profile.model,
        db_path=Path(args.db),
        records=records,
        turn_results=turn_results,
        hermes_home=hermes_home,
        session_id=session_id,
    )
    published = None
    if args.publish_review_run:
        published = _publish_review_run(
            db_path=Path(args.publish_review_db or args.db),
            run_id=args.publish_review_run,
            records=records,
            turn_results=turn_results,
            run_label=run_label,
            model=profile.model,
            debounce_seconds=profile.debounce_seconds,
            turn_offset=args.turn_offset,
        )

    summary = {
        "run_label": run_label,
        "chat_id": args.chat_id,
        "source_table": args.source_table,
        "since_sgt": args.since_sgt,
        "until_sgt": args.until_sgt,
        "messages": len(records),
        "skip_messages": args.skip_messages,
        "processed": processed,
        "turns": len(turn_results),
        "turn_offset": args.turn_offset,
        "profile": profile.name,
        "model": profile.model,
        "vision_provider": profile.vision_provider,
        "vision_model": profile.vision_model,
        "vision_concurrency": profile.vision_concurrency,
        "debounce_seconds": profile.debounce_seconds,
        "session_id": session_id,
        "session_ids": session_ids,
        "session_count": len(session_ids),
        "hermes_home": str(hermes_home),
        "business_base_url": business_base_url,
        "prod_pilot_run_id": args.prod_pilot_run_id,
        "live_business_writes": bool(args.live_business_writes),
        "local_operator_backend": bool(local_backend),
        "openai_capture_dir": os.environ.get("HERMES_OPENAI_CAPTURE_DIR"),
        "html": str(output_path),
        "input_tokens": sum(_as_int(r.get("input_tokens")) for r in turn_results),
        "cached_input_tokens": sum(_as_int(r.get("cached_input_tokens")) for r in turn_results),
        "output_tokens": sum(_as_int(r.get("output_tokens")) for r in turn_results),
        "reasoning_output_tokens": sum(_as_int(r.get("reasoning_output_tokens")) for r in turn_results),
        "estimated_cost_usd": sum(_as_number(r.get("estimated_cost_usd")) for r in turn_results),
        "llm_call_count": sum(_as_int(r.get("llm_call_count")) for r in turn_results),
        "published": published,
        "nightly_compact": nightly_compact_result,
    }
    # Flush explicitly (v6.3 item 5c, WB f6845320): with stdout redirected to
    # a log file the stream is block-buffered, and the day-30 AMK run exited 0
    # with this summary never reaching the log. Flush the JSON itself and
    # drain both streams before returning.
    _finalize_replay_run(hermes_home, _replay_run_row)
    print(json.dumps(summary, indent=2), flush=True)
    if args.cleanup_hermes_home and not args.hermes_home:
        shutil.rmtree(hermes_home, ignore_errors=True)
    sys.stdout.flush()
    sys.stderr.flush()
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="tgg-eval-gpt54-mini", choices=_replay_profile_names())
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--source-table",
        default="bridge_message_log",
        choices=["bridge_message_log", "message_ledger"],
        help="Source table for replay rows. PG prod pilot uses message_ledger.",
    )
    parser.add_argument("--chat-id", default=DEFAULT_CHAT)
    parser.add_argument("--since-sgt", default=DEFAULT_SINCE)
    parser.add_argument("--until-sgt")
    parser.add_argument("--limit-messages", type=int)
    parser.add_argument("--skip-messages", type=int, default=0)
    parser.add_argument("--turn-offset", type=int, default=0)
    parser.add_argument("--debounce-seconds", type=int)
    parser.add_argument("--model")
    parser.add_argument("--vision-provider")
    parser.add_argument("--vision-model")
    parser.add_argument("--vision-concurrency", type=int)
    parser.add_argument("--run-label")
    parser.add_argument("--output")
    parser.add_argument("--hermes-home")
    parser.add_argument("--business-base-url")
    parser.add_argument("--no-local-operator-backend", action="store_true")
    parser.add_argument(
        "--prod-pilot-run-id",
        help=(
            "Explicit bounded-prod pilot mode. Adds X-Replay-Run-Id to business "
            "bridge writes and permits a non-local business URL only with "
            "--live-business-writes."
        ),
    )
    parser.add_argument(
        "--live-business-writes",
        action="store_true",
        help=(
            "Disable HERMES_PA_BUSINESS_DRY_RUN. Intended only with "
            "--prod-pilot-run-id and the systems prod replay gate."
        ),
    )
    parser.add_argument("--publish-review-run")
    parser.add_argument("--publish-review-db")
    parser.add_argument("--render-review-run", help="Render a previously published tgg_christopher_* run without replaying")
    parser.add_argument("--rotate-session-every-turns", type=int)
    parser.add_argument(
        "--continue-session",
        action="store_true",
        help=(
            "Resume the chat's most recent hermes session from a prior run's "
            "hermes-home (same session_id, full history — lets ContextCompressor "
            "auto-compaction fire naturally across multi-day replays). Requires "
            "--hermes-home pointing at the prior run's hermes-home; disables the "
            "wall-clock session reset policy for the run."
        ),
    )
    parser.add_argument(
        "--nightly-compact",
        action="store_true",
        help=(
            "After the day's messages drain, fire a compaction on the chat's "
            "session via the gateway's manual-compress path (same machinery as "
            "/compress: tmp_agent + _compress_context + transcript rewrite under "
            "the new session id). Runs even below the autocompact threshold — "
            "replay emulation of the production 3am scheduled per-session "
            "compact, so the next day's --continue run resumes a compacted "
            "session. Prints a one-line pre/post token + session-id result."
        ),
    )
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    parser.add_argument("--cleanup-hermes-home", action="store_true")
    parser.add_argument(
        "--media-root",
        default=os.environ.get("TGG_REPLAY_MEDIA_ROOT"),
        help=(
            "Directory of restored media files (flat, keyed by basename). When a "
            "bridge_message_log media path no longer exists on disk (e.g. its "
            "originating spec dir was pruned), the harness remaps it to "
            "<media-root>/<basename>. Defaults to $TGG_REPLAY_MEDIA_ROOT."
        ),
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()
    global _MEDIA_ROOT
    if args.media_root:
        _MEDIA_ROOT = Path(args.media_root).expanduser().resolve()
        if not _MEDIA_ROOT.is_dir():
            raise SystemExit(f"--media-root is not a directory: {_MEDIA_ROOT}")
    if args.render_review_run:
        output_path = Path(args.output) if args.output else DOCS_DIR / f"{args.render_review_run}.html"
        summary = _html_report_from_published_run(
            db_path=Path(args.publish_review_db or args.db),
            run_id=args.render_review_run,
            output_path=output_path,
        )
        print(json.dumps(summary, indent=2), flush=True)
        sys.stdout.flush()
        return 0
    _validate_replay_args(args)
    try:
        return asyncio.run(_run(args))
    finally:
        # Belt for the redirected-log loss class (v6.3 item 5c): make sure
        # everything written reaches the log even on exception exits.
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
