from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml

from deploy.pa.provider_key_contract import (
    ProviderKeyContractError,
    assemble,
    load_contract,
    verify,
)


def _spec(
    tmp_path: Path,
    *,
    client: str = "acme",
    openai_slot: str = "OPENAI_API_KEY_ACME",
) -> Path:
    path = tmp_path / "deployment.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "pa.papercutlabs.com/v1",
                "kind": "ClientAgentDeployment",
                "metadata": {"client": client},
                "spec": {
                    "capabilities": {
                        "providerKeys": {
                            "mode": "per-client-principal-supplied",
                            "suppliedBy": "principal",
                            "provenancePath": (
                                "/home/runtime/provider-key-provenance.json"
                            ),
                            "providers": [
                                {
                                    "provider": "openai",
                                    "runtimeEnv": "OPENAI_API_KEY",
                                    "sourceSlot": openai_slot,
                                },
                                {
                                    "provider": "gemini",
                                    "runtimeEnv": "GEMINI_API_KEY",
                                    "sourceSlot": "GEMINI_API_KEY_ACME",
                                },
                            ],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _secrets(tmp_path: Path, *, include_gemini: bool = True) -> Path:
    lines = [
        "OPENAI_API_KEY=fleet-openai",
        "GEMINI_API_KEY=fleet-gemini",
        "OPENAI_API_KEY_ACME=client-openai",
    ]
    if include_gemini:
        lines.append('export GEMINI_API_KEY_ACME="client-gemini"')
    path = tmp_path / "secrets.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_assemble_and_verify_client_slots_without_printing_values(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    secrets = _secrets(tmp_path)
    env_path = tmp_path / "runtime.env"
    provenance = tmp_path / "provider-key-provenance.json"

    result = assemble(
        spec_path=spec,
        secrets_path=secrets,
        output_env=env_path,
        output_provenance=provenance,
    )

    assert result == {
        "client": "acme",
        "mode": "per-client-principal-supplied",
        "sourceSlots": ["OPENAI_API_KEY_ACME", "GEMINI_API_KEY_ACME"],
        "runtimeEnv": ["OPENAI_API_KEY", "GEMINI_API_KEY"],
    }
    assert env_path.read_text() == (
        'OPENAI_API_KEY="client-openai"\n'
        'GEMINI_API_KEY="client-gemini"\n'
    )
    provenance_text = provenance.read_text()
    assert "client-openai" not in provenance_text
    assert "client-gemini" not in provenance_text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(provenance.stat().st_mode) == 0o600

    process = tmp_path / "environ"
    process.write_bytes(
        b"OPENAI_API_KEY=client-openai\0"
        b"GEMINI_API_KEY=client-gemini\0"
    )
    verified = verify(
        spec_path=spec,
        env_path=env_path,
        provenance_path=provenance,
        process_environ_path=process,
    )
    assert verified["status"] == "pass"
    assert verified["liveProcessChecked"] is True
    assert "client-openai" not in json.dumps(verified)
    assert "client-gemini" not in json.dumps(verified)


def test_missing_client_slot_refuses_with_principal_onboarding_step(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ProviderKeyContractError,
        match=(
            "missing required per-client provider slot GEMINI_API_KEY_ACME.*"
            "Teren or Amelia.*fleet keys are forbidden"
        ),
    ):
        assemble(
            spec_path=_spec(tmp_path),
            secrets_path=_secrets(tmp_path, include_gemini=False),
            output_env=tmp_path / "runtime.env",
            output_provenance=tmp_path / "provenance.json",
        )


@pytest.mark.parametrize(
    "source_slot",
    [
        "OPENAI_API_KEY",
        "OPENAI_API_KEY_PCL_PA_SHARED",
        "OPENAI_API_KEY_OTHER_CLIENT",
    ],
)
def test_generic_or_other_client_source_slots_are_refused(
    tmp_path: Path, source_slot: str
) -> None:
    with pytest.raises(
        ProviderKeyContractError,
        match="must source client slot OPENAI_API_KEY_ACME",
    ):
        load_contract(_spec(tmp_path, openai_slot=source_slot))


def test_runtime_env_drift_back_to_fleet_key_is_red(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    env_path = tmp_path / "runtime.env"
    provenance = tmp_path / "provenance.json"
    assemble(
        spec_path=spec,
        secrets_path=_secrets(tmp_path),
        output_env=env_path,
        output_provenance=provenance,
    )
    env_path.write_text(
        'OPENAI_API_KEY="fleet-openai"\n'
        'GEMINI_API_KEY="client-gemini"\n',
        encoding="utf-8",
    )

    with pytest.raises(
        ProviderKeyContractError,
        match="OPENAI_API_KEY does not match assembled client slot",
    ):
        verify(
            spec_path=spec,
            env_path=env_path,
            provenance_path=provenance,
        )


def test_live_process_drift_is_red(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    env_path = tmp_path / "runtime.env"
    provenance = tmp_path / "provenance.json"
    assemble(
        spec_path=spec,
        secrets_path=_secrets(tmp_path),
        output_env=env_path,
        output_provenance=provenance,
    )
    process = tmp_path / "environ"
    process.write_bytes(
        b"OPENAI_API_KEY=fleet-openai\0"
        b"GEMINI_API_KEY=client-gemini\0"
    )

    with pytest.raises(
        ProviderKeyContractError,
        match="live process provider key OPENAI_API_KEY does not match",
    ):
        verify(
            spec_path=spec,
            env_path=env_path,
            provenance_path=provenance,
            process_environ_path=process,
        )
