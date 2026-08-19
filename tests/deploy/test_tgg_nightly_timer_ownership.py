from pathlib import Path


ROOT = Path(__file__).parents[2]
BOOTSTRAP = ROOT / "deploy/tgg/christopher/scripts/bootstrap_runtime.sh"
VERIFY = ROOT / "deploy/tgg/christopher/scripts/verify_runtime.sh"


def test_runtime_deploy_installs_but_does_not_activate_nightly_timer():
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert "christopher-tgg-nightly-whatsapp.timer; do" in bootstrap
    assert "enable --now christopher-tgg-nightly-whatsapp.timer" not in bootstrap


def test_runtime_verifier_does_not_require_nightly_timer_activation():
    verifier = VERIFY.read_text(encoding="utf-8")
    assert "nightly WhatsApp shadow timer must be enabled" not in verifier
