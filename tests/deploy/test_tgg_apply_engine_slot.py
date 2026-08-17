from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import yaml


SCRIPT = (
    Path(__file__).parents[2]
    / "deploy"
    / "tgg"
    / "christopher"
    / "scripts"
    / "apply_engine_slot.py"
)
SPEC = spec_from_file_location("tgg_apply_engine_slot", SCRIPT)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_live_config_uses_runtime_patched_constitution(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    constitution_path = tmp_path / "christopher_tgg_constitution.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "pa": {
                    "constitution_path": (
                        "/runtime/capabilities/christopher-tgg/current/"
                        "christopher_tgg_constitution.yaml"
                    )
                }
            }
        )
    )
    constitution_path.write_text("runtime: {provider: openai-codex}\n")

    MODULE._bind_live_constitution_path(
        config_path,
        constitution_path,
        uid=config_path.stat().st_uid,
        gid=config_path.stat().st_gid,
    )

    config = yaml.safe_load(config_path.read_text())
    assert config["pa"]["constitution_path"] == str(constitution_path)
