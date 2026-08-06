"""Shared fixtures for the hermes-agent test suite.

Hermetic-test invariants enforced here (see AGENTS.md for rationale):

1. **No credential env vars.** All provider/credential-shaped env vars
   (ending in _API_KEY, _TOKEN, _SECRET, _PASSWORD, _CREDENTIALS, etc.)
   are unset before every test. Local developer keys cannot leak in.
2. **Isolated HERMES_HOME.** HERMES_HOME points to a per-test tempdir so
   code reading ``~/.hermes/*`` via ``get_hermes_home()`` can't see the
   real one. (We do NOT also redirect HOME — that broke subprocesses in
   CI. Code using ``Path.home() / ".hermes"`` instead of the canonical
   ``get_hermes_home()`` is a bug to fix at the callsite.)
3. **Deterministic runtime.** TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0.
4. **No HERMES_SESSION_* inheritance** — the agent's current gateway
   session must not leak into tests.

These invariants make the local test run match CI closely. Gaps that
remain (CPU count, xdist worker count) are addressed by the canonical
test runner at ``scripts/run_tests.sh``.
"""

import asyncio
import faulthandler
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from _pytest.stash import StashKey


# Infrastructure-owned threads may be created lazily by pytest/plugins during a
# test and intentionally live for the xdist worker's lifetime.  Test-owned
# threads are deliberately not allowlisted: a new thread must finish or join.
_INFRA_THREAD_NAME_PREFIXES = (
    "acp-agent",
    "agent-evict-",
    "asyncio_",
    "auto-title",
    "bg-review",
    "browser-cleanup",
    "curator-review",
    "pytest-",
    "pydevd.",
    "coverage-",
    "execnet-",
    "hermes-lsp-loop",
    "hermes-tui-notification-",
    "hindsight-loop",
    "hindsight-writer",
    "honcho-async-writer",
    "mcp-event-loop",
    "tirith-install",
    "tui-rpc",
)

# Child discovery through psutil costs ~70 ms per snapshot on macOS.  At two
# snapshots across ~24k tests that would more than double suite time.  Track the
# process-creation primitives instead, then poll only processes actually
# created during a test.  The registry is process-local (one per xdist worker).
_TEST_CHILD_PROCESSES = {}
_TEST_CHILD_PROCESSES_LOCK = threading.Lock()
_PROCESS_STATE_SNAPSHOT = StashKey[dict]()
_MONKEYPATCHED_ENV = StashKey[set[str]]()
_TEST_STARTED_THREADS = StashKey[set[int]]()


def _register_test_child(pid, process=None, name=None):
    if not pid or int(pid) <= 0:
        return
    with _TEST_CHILD_PROCESSES_LOCK:
        _TEST_CHILD_PROCESSES[int(pid)] = (process, name or "child")


def _tracked_live_children():
    live = {}
    stale = []
    with _TEST_CHILD_PROCESSES_LOCK:
        records = list(_TEST_CHILD_PROCESSES.items())
    for pid, (process, name) in records:
        try:
            if process is not None:
                running = process.poll() is None
            else:
                import psutil

                proc = psutil.Process(pid)
                running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            running = False
        if running:
            live[pid] = name
        else:
            stale.append(pid)
    if stale:
        with _TEST_CHILD_PROCESSES_LOCK:
            for pid in stale:
                _TEST_CHILD_PROCESSES.pop(pid, None)
    return live


def _current_loop_state():
    """Return policy/current-loop identity without creating a loop."""
    policy = asyncio.get_event_loop_policy()
    local = getattr(policy, "_local", None)
    current = getattr(local, "_loop", None) if local is not None else None
    if current is not None and current.is_closed():
        current = None
    running = asyncio._get_running_loop()  # no side effect; unlike get_event_loop()
    return (
        policy,
        current,
        current.is_closed() if current is not None else None,
        running,
    )


def _snapshot_call_state():
    return {
        "stdout": sys.stdout,
        "stderr": sys.stderr,
        "event_loop": _current_loop_state(),
    }


def _snapshot_process_state():
    return {
        "threads": {id(thread): thread.name for thread in threading.enumerate()},
        "environment": dict(os.environ),
        "stdout": sys.stdout,
        "stderr": sys.stderr,
        "event_loop": _current_loop_state(),
        "children": _tracked_live_children(),
        "cwd": os.getcwd(),
    }


def _settle_test_threads(item, timeout=0.5):
    """Give test-owned threads that received shutdown time to terminate.

    Async bridges such as Starlette's per-request AnyIO portal can finish their
    request before the worker thread observes its stop signal.  A teardown
    snapshot taken in that scheduling window reports a leak even though the
    thread is already shutting down.  Joining is only a grace period: a thread
    that did not receive a stop signal remains alive and is still reported by
    the process-state guard below.
    """
    started = item.stash.get(_TEST_STARTED_THREADS, frozenset())
    deadline = time.monotonic() + timeout
    for thread in threading.enumerate():
        if id(thread) not in started:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)


def _settle_test_children(timeout=0.5):
    """Give child processes that received shutdown time to exit."""
    with _TEST_CHILD_PROCESSES_LOCK:
        records = list(_TEST_CHILD_PROCESSES.values())
    deadline = time.monotonic() + timeout
    for process, _name in records:
        if process is None or process.poll() is not None:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass


def _environment_state_leaks(before, after, ignored=frozenset()):
    leaks = []
    for name in sorted(set(before) | set(after)):
        if (
            name == "PYTEST_CURRENT_TEST"
            or name in ignored
        ):
            continue
        if name not in before:
            leaks.append(f"left {name} set")
        elif name not in after:
            leaks.append(f"removed {name} from os.environ")
        elif before[name] != after[name]:
            leaks.append(f"changed {name} in os.environ")
    return leaks


def _process_state_leaks(
    before,
    after,
    *,
    check_process_globals=True,
    check_streams=True,
    check_event_loops=True,
    ignored_environment=frozenset(),
    owned_thread_ids=None,
):
    """Return stable, human-readable descriptions of test-owned leftovers."""
    leaks = []

    if check_process_globals:
        new_threads = sorted(
            name
            for ident, name in after["threads"].items()
            if ident not in before["threads"]
            and (owned_thread_ids is None or ident in owned_thread_ids)
            and not name.startswith(_INFRA_THREAD_NAME_PREFIXES)
        )
        leaks.extend(f"left thread {name} running" for name in new_threads)

        leaks.extend(
            _environment_state_leaks(
                before["environment"],
                after["environment"],
                ignored_environment,
            )
        )

    if check_streams:
        if after["stdout"] is not before["stdout"]:
            leaks.append("replaced sys.stdout")
        if after["stderr"] is not before["stderr"]:
            leaks.append("replaced sys.stderr")

    if check_event_loops:
        before_policy, before_current, before_closed, before_running = before[
            "event_loop"
        ]
        after_policy, after_current, after_closed, after_running = after["event_loop"]
        if after_policy is not before_policy:
            leaks.append("replaced event loop policy")
        # asyncio.run() deliberately clears pytest's sync-test loop.  That is
        # not a leftover; fixture teardown restores the sanctioned baseline.
        if after_current is not before_current and after_current is not None:
            leaks.append("left a different current event loop")
        elif after_current is before_current and after_closed != before_closed:
            state = "closed" if after_closed else "open"
            leaks.append(f"left current event loop {state}")
        if after_running is not before_running:
            leaks.append("left a different running event loop")
    if check_process_globals:
        for pid, name in sorted(after["children"].items()):
            if pid not in before["children"]:
                leaks.append(f"left child process {name} (pid {pid}) running")

        if after["cwd"] != before["cwd"]:
            leaks.append(f"changed cwd to {after['cwd']}")

    return leaks


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Snapshot before fixture setup so sanctioned finalizers are inside it."""
    item.stash[_PROCESS_STATE_SNAPSHOT] = _snapshot_process_state()
    item.stash[_TEST_STARTED_THREADS] = set()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    """Catch direct stdio replacement around the call phase.

    Pytest swaps capture streams between phases, so teardown-time identity is
    not meaningful.  The call boundary is stable for capsys and direct writes.
    """
    before = _snapshot_call_state()
    yield
    after = _snapshot_call_state()
    patcher = item.funcargs.get("monkeypatch")
    if patcher is not None:
        item.stash[_MONKEYPATCHED_ENV] = {
            name for target, name, _ in patcher._setitem if target is os.environ
        }
    leaks = _process_state_leaks(
        before,
        after,
        check_process_globals=False,
    )
    if patcher is not None:
        patched = list(patcher._setattr)
        if any(target is sys and name == "stdout" for target, name, _ in patched):
            leaks = [leak for leak in leaks if leak != "replaced sys.stdout"]
        if any(target is sys and name == "stderr" for target, name, _ in patched):
            leaks = [leak for leak in leaks if leak != "replaced sys.stderr"]
    if leaks:
        sys.stdout = before["stdout"]
        sys.stderr = before["stderr"]
        before_policy, before_current, _, _ = before["event_loop"]
        asyncio.set_event_loop_policy(before_policy)
        asyncio.set_event_loop(before_current)
        pytest.fail("test leaked process state:\n- " + "\n- ".join(leaks))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    """Diff after every fixture finalizer and fail the polluting test."""
    yield
    _settle_test_threads(item)
    _settle_test_children()
    before = item.stash[_PROCESS_STATE_SNAPSHOT]
    after = _snapshot_process_state()
    leaks = _process_state_leaks(
        before,
        after,
        check_streams=False,
        check_event_loops=False,
        ignored_environment=item.stash.get(_MONKEYPATCHED_ENV, frozenset()),
        owned_thread_ids=item.stash.get(_TEST_STARTED_THREADS, frozenset()),
    )
    if leaks:
        # Keep the diagnostic from becoming the next test's pollutant.  Threads
        # and children still require normal teardown in the offending test;
        # the cheap process-global surfaces can be restored deterministically.
        sys.stdout = before["stdout"]
        sys.stderr = before["stderr"]
        os.chdir(before["cwd"])
        before_policy, before_current, _, _ = before["event_loop"]
        asyncio.set_event_loop_policy(before_policy)
        asyncio.set_event_loop(before_current)
        pytest.fail("test leaked process state:\n- " + "\n- ".join(leaks))


@pytest.fixture(autouse=True)
def _track_test_child_processes(monkeypatch, request):
    """Track test-owned threads and children without process-table walks."""
    real_thread_start = threading.Thread.start

    def _tracking_thread_start(thread, *args, **kwargs):
        result = real_thread_start(thread, *args, **kwargs)
        request.node.stash[_TEST_STARTED_THREADS].add(id(thread))
        return result

    monkeypatch.setattr(threading.Thread, "start", _tracking_thread_start)
    real_popen_init = subprocess.Popen.__init__

    def _tracking_popen_init(process, *args, **kwargs):
        real_popen_init(process, *args, **kwargs)
        command = args[0] if args else kwargs.get("args", "child")
        if isinstance(command, (list, tuple)) and command:
            command = command[0]
        _register_test_child(process.pid, process, Path(str(command)).name)

    monkeypatch.setattr(subprocess.Popen, "__init__", _tracking_popen_init)

    for spawn_name in ("fork", "posix_spawn", "posix_spawnp"):
        real_spawn = getattr(os, spawn_name, None)
        if real_spawn is None:
            continue

        def _tracking_spawn(*args, _real=real_spawn, _name=spawn_name, **kwargs):
            pid = _real(*args, **kwargs)
            _register_test_child(pid, name=_name)
            return pid

        monkeypatch.setattr(os, spawn_name, _tracking_spawn)

    yield


# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Credential env-var filter ──────────────────────────────────────────────
#
# Any env var in the current process matching ONE of these patterns is
# unset for every test. Developers' local keys cannot leak into assertions
# about "auto-detect provider when key present".

_CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_ACCESS_KEY",
    "_SECRET_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_OAUTH_TOKEN",
    "_WEBHOOK_SECRET",
    "_ENCRYPT_KEY",
    "_APP_SECRET",
    "_CLIENT_SECRET",
    "_CORP_SECRET",
    "_AES_KEY",
)

# Explicit names (for ones that don't fit the suffix pattern)
_CREDENTIAL_NAMES = frozenset({
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_TOKEN",
    "FAL_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "NOUS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "MINIMAX_API_KEY",
    "OLLAMA_API_KEY",
    "OPENVIKING_API_KEY",
    "COPILOT_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "BROWSERBASE_API_KEY",
    "FIRECRAWL_API_KEY",
    "PARALLEL_API_KEY",
    "EXA_API_KEY",
    "TAVILY_API_KEY",
    "WANDB_API_KEY",
    "ELEVENLABS_API_KEY",
    "HONCHO_API_KEY",
    "MEM0_API_KEY",
    "SUPERMEMORY_API_KEY",
    "RETAINDB_API_KEY",
    "HINDSIGHT_API_KEY",
    "HINDSIGHT_LLM_API_KEY",
    "DAYTONA_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "MATTERMOST_TOKEN",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_PASSWORD",
    "MATRIX_RECOVERY_KEY",
    "HASS_TOKEN",
    "EMAIL_PASSWORD",
    "BLUEBUBBLES_PASSWORD",
    "FEISHU_APP_SECRET",
    "FEISHU_ENCRYPT_KEY",
    "FEISHU_VERIFICATION_TOKEN",
    "DINGTALK_CLIENT_SECRET",
    "QQ_CLIENT_SECRET",
    "QQ_STT_API_KEY",
    "WECOM_SECRET",
    "WECOM_CALLBACK_CORP_SECRET",
    "WECOM_CALLBACK_TOKEN",
    "WECOM_CALLBACK_ENCODING_AES_KEY",
    "WEIXIN_TOKEN",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "TERMINAL_SSH_KEY",
    "SUDO_PASSWORD",
    "GATEWAY_PROXY_KEY",
    "API_SERVER_KEY",
    "TOOL_GATEWAY_USER_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "WEBHOOK_SECRET",
    "AI_GATEWAY_API_KEY",
    "VOICE_TOOLS_OPENAI_KEY",
    "BROWSER_USE_API_KEY",
    "CUSTOM_API_KEY",
    "GATEWAY_PROXY_URL",
    "GEMINI_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    "OLLAMA_BASE_URL",
    "GROQ_BASE_URL",
    "XAI_BASE_URL",
    "AI_GATEWAY_BASE_URL",
    "ANTHROPIC_BASE_URL",
})


def _looks_like_credential(name: str) -> bool:
    """True if env var name matches a credential-shaped pattern."""
    if name in _CREDENTIAL_NAMES:
        return True
    return any(name.endswith(suf) for suf in _CREDENTIAL_SUFFIXES)


# HERMES_* vars that change test behavior by being set. Unset all of these
# unconditionally — individual tests that need them set do so explicitly.
_HERMES_BEHAVIORAL_VARS = frozenset({
    "HERMES_YOLO_MODE",
    "HERMES_INTERACTIVE",
    "HERMES_QUIET",
    "HERMES_TOOL_PROGRESS",
    "HERMES_TOOL_PROGRESS_MODE",
    "HERMES_MAX_ITERATIONS",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_NAME",
    "HERMES_SESSION_THREAD_ID",
    "HERMES_SESSION_ID",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_KEY",
    "HERMES_GATEWAY_SESSION",
    "HERMES_PLATFORM",
    "HERMES_MODEL",
    "HERMES_INFERENCE_MODEL",
    "HERMES_INFERENCE_PROVIDER",
    "HERMES_TUI_PROVIDER",
    "HERMES_MANAGED",
    "HERMES_DEV",
    "HERMES_CONTAINER",
    "HERMES_EPHEMERAL_SYSTEM_PROMPT",
    "HERMES_TIMEZONE",
    "HERMES_REDACT_SECRETS",
    "HERMES_BACKGROUND_NOTIFICATIONS",
    "HERMES_EXEC_ASK",
    "HERMES_HOME_MODE",
    # Kanban path/board pins must never leak from a developer shell or
    # dispatched worker into tests; otherwise tests can write fake tasks to
    # the real ~/.hermes/kanban.db instead of the per-test HERMES_HOME.
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_LOGS_ROOT",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_TENANT",
    "TERMINAL_CWD",
    "TERMINAL_ENV",
    "TERMINAL_VERCEL_RUNTIME",
    "TERMINAL_CONTAINER_CPU",
    "TERMINAL_CONTAINER_DISK",
    "TERMINAL_CONTAINER_MEMORY",
    "TERMINAL_CONTAINER_PERSISTENT",
    "TERMINAL_DOCKER_RUN_AS_HOST_USER",
    "BROWSER_CDP_URL",
    "CAMOFOX_URL",
    # Platform allowlists — not credentials, but if set from any source
    # (user shell, earlier leaky test, CI env), they change gateway auth
    # behavior and flake button-authorization tests.
    "TELEGRAM_ALLOWED_USERS",
    "DISCORD_ALLOWED_USERS",
    "WHATSAPP_ALLOWED_USERS",
    "SLACK_ALLOWED_USERS",
    "SIGNAL_ALLOWED_USERS",
    "SIGNAL_GROUP_ALLOWED_USERS",
    "EMAIL_ALLOWED_USERS",
    "SMS_ALLOWED_USERS",
    "MATTERMOST_ALLOWED_USERS",
    "MATRIX_ALLOWED_USERS",
    "DINGTALK_ALLOWED_USERS",
    "FEISHU_ALLOWED_USERS",
    "WECOM_ALLOWED_USERS",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "DISCORD_ALLOW_ALL_USERS",
    "WHATSAPP_ALLOW_ALL_USERS",
    "SLACK_ALLOW_ALL_USERS",
    "SIGNAL_ALLOW_ALL_USERS",
    "EMAIL_ALLOW_ALL_USERS",
    "SMS_ALLOW_ALL_USERS",
    # Gateway home channels are set by /sethome in real profiles. Tests that
    # exercise dashboard notification toggles must opt in explicitly or they
    # can accidentally subscribe against a developer's real home channel.
    "TELEGRAM_HOME_CHANNEL",
    "TELEGRAM_HOME_CHANNEL_THREAD_ID",
    "TELEGRAM_HOME_CHANNEL_NAME",
    "DISCORD_HOME_CHANNEL",
    "DISCORD_HOME_CHANNEL_THREAD_ID",
    "DISCORD_HOME_CHANNEL_NAME",
    "SLACK_HOME_CHANNEL",
    "SLACK_HOME_CHANNEL_THREAD_ID",
    "SLACK_HOME_CHANNEL_NAME",
    "WHATSAPP_HOME_CHANNEL",
    "WHATSAPP_HOME_CHANNEL_THREAD_ID",
    "WHATSAPP_HOME_CHANNEL_NAME",
    "SIGNAL_HOME_CHANNEL",
    "SIGNAL_HOME_CHANNEL_THREAD_ID",
    "SIGNAL_HOME_CHANNEL_NAME",
    "EMAIL_HOME_CHANNEL",
    "EMAIL_HOME_CHANNEL_THREAD_ID",
    "EMAIL_HOME_CHANNEL_NAME",
    "SMS_HOME_CHANNEL",
    "SMS_HOME_CHANNEL_THREAD_ID",
    "SMS_HOME_CHANNEL_NAME",
    "MATTERMOST_HOME_CHANNEL",
    "MATTERMOST_HOME_CHANNEL_THREAD_ID",
    "MATTERMOST_HOME_CHANNEL_NAME",
    "MATRIX_HOME_CHANNEL",
    "MATRIX_HOME_CHANNEL_THREAD_ID",
    "MATRIX_HOME_CHANNEL_NAME",
    "DINGTALK_HOME_CHANNEL",
    "DINGTALK_HOME_CHANNEL_THREAD_ID",
    "DINGTALK_HOME_CHANNEL_NAME",
    "FEISHU_HOME_CHANNEL",
    "FEISHU_HOME_CHANNEL_THREAD_ID",
    "FEISHU_HOME_CHANNEL_NAME",
    "WECOM_HOME_CHANNEL",
    "WECOM_HOME_CHANNEL_THREAD_ID",
    "WECOM_HOME_CHANNEL_NAME",
    # Platform gating — set by load_gateway_config() as a side effect when
    # a config.yaml is present, so individual test bodies that call the
    # loader leak these values into later tests on the same xdist worker.
    # Force-clear on every test setup so the leak can't happen.
    "SLACK_REQUIRE_MENTION",
    "SLACK_STRICT_MENTION",
    "SLACK_FREE_RESPONSE_CHANNELS",
    "SLACK_ALLOW_BOTS",
    "SLACK_REACTIONS",
    "DISCORD_REQUIRE_MENTION",
    "DISCORD_FREE_RESPONSE_CHANNELS",
    "TELEGRAM_REQUIRE_MENTION",
    "WHATSAPP_REQUIRE_MENTION",
    "DINGTALK_REQUIRE_MENTION",
    "MATRIX_REQUIRE_MENTION",
})


@pytest.fixture(autouse=True)
def _hermetic_environment(tmp_path, monkeypatch, request):
    """Blank out all credential/behavioral env vars so local and CI match.

    Also redirects HOME and HERMES_HOME to per-test tempdirs so code that
    reads ``~/.hermes/*`` can't touch the real one, and pins TZ/LANG so
    datetime/locale-sensitive tests are deterministic.
    """
    initial_environment = dict(os.environ)

    # 1. Blank every credential-shaped env var that's currently set.
    for name in list(os.environ.keys()):
        if _looks_like_credential(name):
            monkeypatch.delenv(name, raising=False)

    # 2. Blank behavioral HERMES_* vars that could change test semantics.
    for name in _HERMES_BEHAVIORAL_VARS:
        monkeypatch.delenv(name, raising=False)

    # 3. Redirect HERMES_HOME to a per-test tempdir. Code that reads
    #    ``~/.hermes/*`` via ``get_hermes_home()`` now gets the tempdir.
    #
    #    NOTE: We do NOT also redirect HOME. Doing so broke CI because
    #    some tests (and their transitive deps) spawn subprocesses that
    #    inherit HOME and expect it to be stable. If a test genuinely
    #    needs HOME isolated, it should set it explicitly in its own
    #    fixture. Any code in the codebase reading ``~/.hermes/*`` via
    #    ``Path.home() / ".hermes"`` instead of ``get_hermes_home()``
    #    is a bug to fix at the callsite.
    fake_hermes_home = tmp_path / "hermes_test"
    fake_hermes_home.mkdir()
    (fake_hermes_home / "sessions").mkdir()
    (fake_hermes_home / "cron").mkdir()
    (fake_hermes_home / "memories").mkdir()
    (fake_hermes_home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(fake_hermes_home))

    # 4. Deterministic locale / timezone / hashseed. CI runs in UTC with
    #    C.UTF-8 locale; local dev often doesn't. Pin everything.
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("PYTHONHASHSEED", "0")

    # 4b. Disable AWS IMDS lookups. Without this, any test that ends up
    #     calling has_aws_credentials() / resolve_aws_auth_env_var()
    #     (e.g. provider auto-detect, status command, cron run_job) burns
    #     ~2s waiting for the metadata service at 169.254.169.254 to time
    #     out. Tests don't run on EC2 — IMDS is always unreachable here.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_METADATA_SERVICE_TIMEOUT", "1")
    monkeypatch.setenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")

    # 5. Reset plugin singleton so tests don't leak plugins from
    #    ~/.hermes/plugins/ (which, per step 3, is now empty — but the
    #    singleton might still be cached from a previous test).
    try:
        import hermes_cli.plugins as _plugins_mod
        monkeypatch.setattr(_plugins_mod, "_plugin_manager", None)
    except Exception:
        pass
    # Explicitly clear provider-specific base URL overrides that don't match
    # the generic credential-shaped env-var filter above.
    monkeypatch.delenv("GMI_API_KEY", raising=False)
    monkeypatch.delenv("GMI_BASE_URL", raising=False)

    yield

    # Production config loaders intentionally bridge settings into os.environ.
    # Tests must not export those runtime settings to the next xdist item.  A
    # whole-map restore catches new bridge keys without growing another stale
    # denylist and prevents a failed test from contaminating the next item.
    os.environ.clear()
    os.environ.update(initial_environment)


# Backward-compat alias — old tests reference this fixture name. Keep it
# as a no-op wrapper so imports don't break.
@pytest.fixture(autouse=True)
def _isolate_hermes_home(_hermetic_environment):
    """Alias preserved for any test that yields this name explicitly."""
    return None


# ── Module-level state reset ───────────────────────────────────────────────
#
# Python modules are singletons per process, and pytest-xdist workers are
# long-lived. Module-level dicts/sets (tool registries, approval state,
# interrupt flags) and ContextVars persist across tests in the same worker,
# causing tests that pass alone to fail when run with siblings.
#
# Each entry in this fixture clears state that belongs to a specific module.
# New state buckets go here too — this is the single gate that prevents
# "works alone, flakes in CI" bugs from state leakage.
#
# The skill `test-suite-cascade-diagnosis` documents the concrete patterns
# this closes; the running example was `test_command_guards` failing 12/15
# CI runs because ``tools.approval._session_approved`` carried approvals
# from one test's session into another's.

@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear module-level mutable state and ContextVars between tests.

    Keeps state from leaking across tests on the same xdist worker. Modules
    that don't exist yet (test collection before production import) are
    skipped silently — production import later creates fresh empty state.
    """
    # --- logging — quiet/one-shot paths mutate process-global logger state ---
    logging.disable(logging.NOTSET)
    for _logger_name in ("tools", "run_agent", "trajectory_compressor", "cron", "hermes_cli"):
        _logger = logging.getLogger(_logger_name)
        _logger.disabled = False
        _logger.setLevel(logging.NOTSET)
        _logger.propagate = True

    # --- tools.approval — the single biggest source of cross-test pollution ---
    try:
        from tools import approval as _approval_mod
        _approval_mod._session_approved.clear()
        _approval_mod._session_yolo.clear()
        _approval_mod._permanent_approved.clear()
        _approval_mod._pending.clear()
        _approval_mod._gateway_queues.clear()
        _approval_mod._gateway_notify_cbs.clear()
        # ContextVar: reset to empty string so get_current_session_key()
        # falls through to the env var / default path, matching a fresh
        # process.
        _approval_mod._approval_session_key.set("")
    except Exception:
        pass

    # --- tools.interrupt — per-thread interrupt flag set ---
    try:
        from tools import interrupt as _interrupt_mod
        with _interrupt_mod._lock:
            _interrupt_mod._interrupted_threads.clear()
    except Exception:
        pass

    # --- gateway.session_context — 9 ContextVars that represent
    #     the active gateway session. If set in one test and not reset,
    #     the next test's get_session_env() reads stale values.
    try:
        from gateway import session_context as _sc_mod
        for _cv in (
            _sc_mod._SESSION_PLATFORM,
            _sc_mod._SESSION_CHAT_ID,
            _sc_mod._SESSION_CHAT_NAME,
            _sc_mod._SESSION_THREAD_ID,
            _sc_mod._SESSION_USER_ID,
            _sc_mod._SESSION_USER_NAME,
            _sc_mod._SESSION_KEY,
            _sc_mod._CRON_AUTO_DELIVER_PLATFORM,
            _sc_mod._CRON_AUTO_DELIVER_CHAT_ID,
            _sc_mod._CRON_AUTO_DELIVER_THREAD_ID,
        ):
            _cv.set(_sc_mod._UNSET)
    except Exception:
        pass

    # --- tools.env_passthrough — ContextVar<set[str]> with no default ---
    # LookupError is normal if the test never set it. Setting it to an
    # empty set unconditionally normalizes the starting state.
    try:
        from tools import env_passthrough as _envp_mod
        _envp_mod._allowed_env_vars_var.set(set())
    except Exception:
        pass

    # --- tools.terminal_tool — active environment/cwd cache ---
    # File tools prefer a live terminal cwd when one is cached for the task.
    # Clear terminal environments between tests so a prior terminal call can't
    # override TERMINAL_CWD in path-resolution tests.
    try:
        from tools import terminal_tool as _term_mod
        _envs_to_cleanup = []
        with _term_mod._env_lock:
            _envs_to_cleanup = list(_term_mod._active_environments.values())
            _term_mod._active_environments.clear()
            _term_mod._last_activity.clear()
            _term_mod._creation_locks.clear()
        for _env in _envs_to_cleanup:
            try:
                _env.cleanup()
            except Exception:
                pass
    except Exception:
        pass

    # --- tools.credential_files — ContextVar<dict> ---
    try:
        from tools import credential_files as _credf_mod
        _credf_mod._registered_files_var.set({})
    except Exception:
        pass

    # --- agent.auxiliary_client — runtime main provider/model override and
    #     payment-error health cache. Both are process-global in production;
    #     reset them per test so one worker's fallback/402 test does not make
    #     later auxiliary-client tests skip otherwise-available providers.
    try:
        from agent import auxiliary_client as _aux_mod
        _aux_mod.clear_runtime_main()
        _aux_mod._reset_aux_unhealthy_cache()
    except Exception:
        pass

    # --- tools.file_tools — per-task read history + file-ops cache ---
    # _read_tracker accumulates per-task_id read history for loop detection,
    # capped by _READ_HISTORY_CAP. If entries from a prior test persist, the
    # cap is hit faster than expected and capacity-related tests flake.
    try:
        from tools import file_tools as _ft_mod
        with _ft_mod._read_tracker_lock:
            _ft_mod._read_tracker.clear()
        with _ft_mod._file_ops_lock:
            _ft_mod._file_ops_cache.clear()
    except Exception:
        pass

    yield

    # Process-lifetime cleanup workers are valid in Hermes, but tests that
    # start one must stop it before yielding the xdist worker to another test.
    _term_mod = sys.modules.get("tools.terminal_tool")
    if _term_mod is not None:
        _term_mod._stop_cleanup_thread()
        _term_mod._cleanup_thread = None
    _browser_mod = sys.modules.get("tools.browser_tool")
    if _browser_mod is not None:
        _browser_mod._stop_browser_cleanup_thread()
        _browser_mod._cleanup_thread = None


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal hermes config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Global test timeout ─────────────────────────────────────────────────────
# Kill any individual test that takes longer than 30 seconds.
# Prevents hanging tests (subprocess spawns, blocking I/O) from stalling the
# entire test suite.

def _timeout_handler(signum, frame):
    try:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception as exc:
        print(f"failed to dump all thread stacks: {exc}", file=sys.stderr)
    thread_names = sorted(thread.name for thread in threading.enumerate())
    print(
        "live threads at test timeout: " + ", ".join(thread_names),
        file=sys.stderr,
        flush=True,
    )
    raise TimeoutError("Test exceeded 30 second timeout")

@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.

    On Python 3.12+, ``asyncio.get_event_loop_policy().get_event_loop()`` with no
    *running* loop emits DeprecationWarning; skip that path and install a fresh
    loop via ``new_event_loop()`` instead.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    loop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    if loop is None:
        # ``policy.get_event_loop()`` creates a loop as a side effect on
        # Python 3.11, making a framework-owned loop look test-owned and then
        # leaving it current forever.  Inspect the policy-local slot instead.
        _, loop, _, _ = _current_loop_state()

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Kill any individual test that takes longer than 30 seconds.
    SIGALRM is Unix-only; skip on Windows."""
    if sys.platform == "win32":
        yield
        return
    old = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(30)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


@pytest.fixture(autouse=True)
def _reset_tool_registry_caches():
    """Clear tool-registry-level caches between tests.

    The production registry caches ``check_fn()`` results for 30 s
    (see tools/registry.py) and :func:`get_tool_definitions` memoizes
    its result (see model_tools.py). Both are keyed on state that tests
    routinely mutate (env vars, registry._generation, config.yaml mtime)
    — but a stale result from test A can still be served to test B
    because 30 s covers the entire suite, and xdist worker reuse means
    one test's cache lands in another's process. Clearing before every
    test keeps hermetic behavior.
    """
    try:
        from tools.registry import invalidate_check_fn_cache
        invalidate_check_fn_cache()
    except ImportError:
        pass
    try:
        from model_tools import _clear_tool_defs_cache
        _clear_tool_defs_cache()
    except ImportError:
        pass


# ── Live-system guard ──────────────────────────────────────────────────────
#
# Several test files exercise the gateway-restart / kill code paths
# (``cmd_update``, ``kill_gateway_processes``, ``stop_profile_gateway``).
# When a single test forgets to mock either ``os.kill`` or the global
# ``find_gateway_pids`` helper, the real call leaks out of the hermetic
# environment and finds the developer's live ``hermes-gateway`` process
# via ``psutil`` — sending it SIGTERM mid-test. The shutdown forensics in
# PR #23285 caught this happening 5+ times in 3 days, every time
# correlated with a ``tests/hermes_cli/`` pytest run starting up.
#
# This fixture makes the leak impossible by intercepting the two
# primitives that actually do damage:
#
#  • ``os.kill`` rejects any PID outside the test process subtree with
#    a hard ``RuntimeError`` so the offending test gets a stack trace
#    instead of silently murdering the real gateway.
#  • ``subprocess.run`` / ``subprocess.Popen`` / ``call`` / ``check_call`` /
#    ``check_output`` reject any ``systemctl ... <verb> hermes-gateway``
#    invocation that would mutate the live unit. Read-only systemctl
#    calls (``status``, ``show``, ``list-units``) still pass through.
#
# We intentionally do NOT stub ``find_gateway_pids`` / ``_scan_gateway_pids``
# here — tests of those functions themselves need the real implementation.
# Even if a test gets the live gateway PID back from a real scan, the
# ``os.kill`` guard above catches the actual signal call, and the
# ``systemctl`` guard catches the systemd path. Discovery without
# delivery is harmless.

_LIVE_SYSTEM_GUARD_BYPASS_MARK = "live_system_guard_bypass"


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register markers used by hermetic conftest."""
    config.addinivalue_line(
        "markers",
        f"{_LIVE_SYSTEM_GUARD_BYPASS_MARK}: bypass the live-system guard "
        "(only for tests that genuinely need real os.kill / subprocess "
        "behaviour — e.g. PTY tests that signal their own child).",
    )


@pytest.fixture(autouse=True)
def _live_system_guard(request, monkeypatch):
    """Block real os.kill / systemctl / gateway-pid scans during tests.

    See block comment above for the why. Tests that genuinely need
    real signal delivery (e.g. PTY tests that SIGINT their own child)
    can opt out with ``@pytest.mark.live_system_guard_bypass``.

    Coverage (every primitive that can deliver a signal to or otherwise
    terminate a foreign process):
      • os.kill, os.killpg (POSIX)
      • subprocess.run / Popen / call / check_call / check_output
      • subprocess.getoutput / getstatusoutput
      • os.system / os.popen
      • pty.spawn
      • asyncio.create_subprocess_exec / create_subprocess_shell
    Subprocess inspection looks at the WHOLE command string (not just
    tokens[0]), so ``bash -c "systemctl restart hermes-gateway"``,
    ``sudo systemctl ...``, ``env systemctl ...``, ``setsid systemctl ...``
    are all caught. ``pkill``/``killall``/``taskkill`` invocations
    targeting hermes/python patterns are also blocked.
    """
    if request.node.get_closest_marker(_LIVE_SYSTEM_GUARD_BYPASS_MARK):
        yield
        return

    import os as _os
    import shlex as _shlex
    import subprocess as _subprocess

    test_pid = _os.getpid()
    # Capture the test process's existing children at fixture start —
    # any *new* children spawned by the test are also allowlisted via
    # the live psutil walk below. Static set keeps the fast path cheap.
    try:
        import psutil as _psutil
        _initial_children = {
            c.pid for c in _psutil.Process(test_pid).children(recursive=True)
        }
    except Exception:
        _psutil = None
        _initial_children = set()

    def _is_own_subtree(pid: int) -> bool:
        # PID 0 means "our own process group"; -1 means "every process we
        # can signal". Both are dangerous when paired with SIGTERM/SIGKILL,
        # but pid 0 is technically scoped to our group so allow it; pid -1
        # is treated as foreign (refuse).
        if pid == 0:
            return True
        if pid < 0:
            return False
        if pid == test_pid or pid in _initial_children:
            return True
        if _psutil is None:
            return False
        try:
            walker = _psutil.Process(pid)
        except Exception:
            # Stale PID — kill would be a no-op anyway, allow it.
            return True
        try:
            for parent in walker.parents():
                if parent.pid == test_pid:
                    return True
        except Exception:
            return False
        return False

    real_kill = _os.kill

    def _guarded_kill(pid, sig, *args, **kwargs):
        if _is_own_subtree(int(pid)):
            return real_kill(pid, sig, *args, **kwargs)
        raise RuntimeError(
            f"tests/conftest.py live-system guard: blocked os.kill("
            f"{pid}, {sig}) — PID is outside the test process subtree. "
            "If this fired in CI it means the test reached a real "
            "kill_gateway_processes / stop_profile_gateway / cmd_update "
            "code path without mocking find_gateway_pids and os.kill. "
            "Mock both, or mark the test with "
            "@pytest.mark.live_system_guard_bypass if real signal "
            "delivery is genuinely required."
        )

    monkeypatch.setattr(_os, "kill", _guarded_kill)

    # ``os.killpg`` is the same risk class — sends a signal to every
    # process in a group. The gateway is a session leader (its own
    # PGID == its PID), so killpg(gateway_pid, SIGTERM) is a one-shot
    # kill of the live process. Allow it only when the target PGID is
    # the test process's own group.
    if hasattr(_os, "killpg"):
        real_killpg = _os.killpg
        own_pgid = _os.getpgrp()

        def _guarded_killpg(pgid, sig, *args, **kwargs):
            if int(pgid) == own_pgid or _is_own_subtree(int(pgid)):
                return real_killpg(pgid, sig, *args, **kwargs)
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"os.killpg({pgid}, {sig}) — PGID is outside the test "
                "process group. See _live_system_guard for the why."
            )

        monkeypatch.setattr(_os, "killpg", _guarded_killpg)

    # ── Subprocess command-string inspection (whole-line) ──────────
    _HERMES_TOKENS = (
        "hermes-gateway",
        "hermes.service",
        "hermes_cli.main gateway",
        "hermes_cli/main.py gateway",
        "gateway/run.py",
        "hermes gateway",
    )
    _MUTATING_VERBS = (
        "restart", "start", "stop", "kill", "reload",
        "reset-failed", "enable", "disable", "mask", "unmask",
        "daemon-reload", "try-restart", "reload-or-restart",
    )
    _PROCESS_KILLERS = ("pkill", "killall", "taskkill", "skill", "fuser")

    def _cmd_to_string(cmd) -> str:
        if cmd is None:
            return ""
        if isinstance(cmd, (bytes, bytearray)):
            try:
                return bytes(cmd).decode(errors="replace")
            except Exception:
                return ""
        if isinstance(cmd, str):
            return cmd
        if isinstance(cmd, (list, tuple)):
            try:
                return " ".join(str(t) for t in cmd)
            except Exception:
                return ""
        return str(cmd)

    def _matches_hermes_gateway(cmd_str: str) -> bool:
        low = cmd_str.lower()
        return any(tok in low for tok in _HERMES_TOKENS)

    def _is_blocked_systemctl(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        if "systemctl" not in cmd_str:
            return False
        if not _matches_hermes_gateway(cmd_str):
            return False
        try:
            tokens = _shlex.split(cmd_str)
        except ValueError:
            tokens = cmd_str.split()
        return any(verb in tokens for verb in _MUTATING_VERBS)

    def _is_process_killer(cmd) -> bool:
        cmd_str = _cmd_to_string(cmd)
        try:
            tokens = _shlex.split(cmd_str)
        except ValueError:
            tokens = cmd_str.split()
        if not tokens:
            return False
        for tok in tokens:
            head = tok.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if head in _PROCESS_KILLERS:
                low = cmd_str.lower()
                # pkill -f pattern: catch hermes-themed patterns + a
                # plain "python" -f which would catch the live gateway
                # whose cmdline contains "python -m hermes_cli.main".
                if (
                    "hermes" in low
                    or "gateway" in low
                    or ("python" in low and "-f" in tokens)
                ):
                    return True
        return False

    def _check_subprocess_cmd(name, cmd):
        if _is_blocked_systemctl(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — would mutate the "
                "live hermes-gateway systemd unit. Mock "
                "subprocess.run / _run_systemctl in the test, or "
                "mark with @pytest.mark.live_system_guard_bypass."
            )
        if _is_process_killer(cmd):
            raise RuntimeError(
                f"tests/conftest.py live-system guard: blocked "
                f"subprocess.{name}({cmd!r}) — process-killer command "
                "targeting hermes/python could hit the live gateway. "
                "Mark with @pytest.mark.live_system_guard_bypass if "
                "intentional."
            )

    def _wrap_subprocess(name, real):
        def _guarded(cmd, *args, **kwargs):
            _check_subprocess_cmd(name, cmd)
            return real(cmd, *args, **kwargs)
        _guarded.__name__ = f"_guarded_{name}"
        # Make the wrapper subscriptable like the wrapped callable when
        # the wrapped object is. ``subprocess.Popen[bytes]`` is used as
        # a type annotation in third-party packages (mcp, etc.); replacing
        # ``Popen`` with a plain function breaks ``Popen[bytes]`` at
        # import time. Defer ``__class_getitem__`` to the original.
        if hasattr(real, "__class_getitem__"):
            _guarded.__class_getitem__ = real.__class_getitem__
        return _guarded

    def _wrap_popen():
        """Subclass Popen so isinstance checks AND Popen[bytes] still work."""
        real = _subprocess.Popen

        class _GuardedPopen(real):  # type: ignore[misc, valid-type]
            def __init__(self, cmd, *args, **kwargs):
                _check_subprocess_cmd("Popen", cmd)
                super().__init__(cmd, *args, **kwargs)

        _GuardedPopen.__name__ = "Popen"
        _GuardedPopen.__qualname__ = "Popen"
        return _GuardedPopen

    real_run = _subprocess.run
    real_popen = _subprocess.Popen
    real_call = _subprocess.call
    real_check_call = _subprocess.check_call
    real_check_output = _subprocess.check_output
    real_getoutput = _subprocess.getoutput
    real_getstatusoutput = _subprocess.getstatusoutput

    monkeypatch.setattr(_subprocess, "run", _wrap_subprocess("run", real_run))
    monkeypatch.setattr(_subprocess, "Popen", _wrap_popen())
    monkeypatch.setattr(_subprocess, "call", _wrap_subprocess("call", real_call))
    monkeypatch.setattr(
        _subprocess, "check_call", _wrap_subprocess("check_call", real_check_call)
    )
    monkeypatch.setattr(
        _subprocess,
        "check_output",
        _wrap_subprocess("check_output", real_check_output),
    )
    monkeypatch.setattr(
        _subprocess, "getoutput", _wrap_subprocess("getoutput", real_getoutput)
    )
    monkeypatch.setattr(
        _subprocess,
        "getstatusoutput",
        _wrap_subprocess("getstatusoutput", real_getstatusoutput),
    )

    # os.system / os.popen — same risk class, completely unwrapped before.
    real_os_system = _os.system
    real_os_popen = _os.popen

    def _guarded_os_system(command):
        _check_subprocess_cmd("os.system", command)
        return real_os_system(command)

    def _guarded_os_popen(cmd, *args, **kwargs):
        _check_subprocess_cmd("os.popen", cmd)
        return real_os_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(_os, "system", _guarded_os_system)
    monkeypatch.setattr(_os, "popen", _guarded_os_popen)

    # pty.spawn — POSIX-only.
    try:
        import pty as _pty
        if hasattr(_pty, "spawn"):
            real_pty_spawn = _pty.spawn

            def _guarded_pty_spawn(argv, *args, **kwargs):
                _check_subprocess_cmd("pty.spawn", argv)
                return real_pty_spawn(argv, *args, **kwargs)

            monkeypatch.setattr(_pty, "spawn", _guarded_pty_spawn)
    except Exception:
        pass

    # asyncio.create_subprocess_* — bypasses subprocess module entirely.
    try:
        import asyncio as _asyncio
        real_async_exec = _asyncio.create_subprocess_exec
        real_async_shell = _asyncio.create_subprocess_shell

        async def _guarded_async_exec(program, *args, **kwargs):
            _check_subprocess_cmd(
                "asyncio.create_subprocess_exec", [program, *args]
            )
            return await real_async_exec(program, *args, **kwargs)

        async def _guarded_async_shell(cmd, *args, **kwargs):
            _check_subprocess_cmd("asyncio.create_subprocess_shell", cmd)
            return await real_async_shell(cmd, *args, **kwargs)

        monkeypatch.setattr(_asyncio, "create_subprocess_exec", _guarded_async_exec)
        monkeypatch.setattr(
            _asyncio, "create_subprocess_shell", _guarded_async_shell
        )
    except Exception:
        pass

    yield
