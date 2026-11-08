"""HT fork patch P6 (HT-PATCHES.md): a failed ``transform_tool_result`` withholds the tool result,
behind ``ht_patches.p6_tool_result_transform_fail_closed`` (default off).

Upstream skips a ``transform_tool_result`` callback that raises, times out or is skipped (still
running, suppressed after a timeout), and returns the untransformed result when the transform step
itself raises. A transform that redacts (the HLM Harness plugin strips the job capability there)
is then bypassed. Each failure is exercised on each path with the flag off (upstream: the raw
result reaches the model) and on (the result is withheld): ``model_tools.handle_function_call``,
the agent's sequential and concurrent loops, ``AIAgent._invoke_tool`` and the inline-executor
helper ``apply_transform_tool_result``.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import ht_patches
from hermes_cli.plugins import PluginManager
from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call

SECRET = "htc_" + "0123456789abcdef" * 2
RAW = json.dumps({"thread": "t-1", "echo": SECRET})
REDACTED = RAW.replace(SECRET, "[REDACTED]")
WITHHELD = re.compile(r"BLOCKED: the transform_tool_result hook failed for web_search \(([^)]*)\); "
                      r"the result was withheld")
FLAG = "p6_tool_result_transform_fail_closed"
OFF_SECTIONS = [None, {}, {FLAG: False}, {"p1_run_hook_kwargs": True, "p5_hook_dispatch_fail_closed": True}]
ON_SECTIONS = [{FLAG: True},
               {"p1_run_hook_kwargs": True, "p5_hook_dispatch_fail_closed": True, FLAG: True}]


@pytest.fixture()
def home():
    return Path(os.environ["HERMES_HOME"])


def _write_config(home, section, timeout=None):
    config = {} if section is None else {"ht_patches": section}
    if timeout is not None:
        config["plugins"] = {"hook_callback_timeout": timeout}
    (home / "config.yaml").write_text(json.dumps(config))


def _redact(result=None, **_):
    return None if SECRET not in str(result) else str(result).replace(SECRET, "[REDACTED]")


def _raises(**_):
    raise RuntimeError("transform broke near " + SECRET)


def _slow(**_):
    time.sleep(1.5)
    return "late"


def _install(monkeypatch, *callbacks):
    import hermes_cli.plugins as plugins_mod
    mgr = PluginManager()
    mgr._discovered = True
    mgr._hooks["transform_tool_result"] = list(callbacks)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return mgr


def _assert_withheld(text: str, reason: str):
    match = WITHHELD.search(text)
    assert match, text
    assert match.group(1) == reason
    assert SECRET not in text


# --- model_tools.handle_function_call -------------------------------------------------------------

def _handle():
    registry = MagicMock()
    registry.dispatch.return_value = RAW
    with patch("model_tools.registry", registry):
        from model_tools import handle_function_call
        return handle_function_call("web_search", {"query": "q"}, task_id="t1", session_id="s1",
                                    tool_call_id="call-1")


@pytest.mark.parametrize("section", OFF_SECTIONS)
def test_p6_off_a_raising_transform_passes_the_raw_result(home, monkeypatch, section):
    _write_config(home, section)
    _install(monkeypatch, _raises)
    assert _handle() == RAW  # upstream: skipped, untransformed


@pytest.mark.parametrize("section", ON_SECTIONS)
def test_p6_on_a_raising_transform_withholds_the_result(home, monkeypatch, section):
    _write_config(home, section)
    _install(monkeypatch, _raises)
    result = _handle()
    _assert_withheld(result, "RuntimeError")
    assert isinstance(result, ht_patches.ResultWithheld)


@pytest.mark.parametrize("section, withheld", [(OFF_SECTIONS[2], False), (ON_SECTIONS[0], True)])
def test_p6_a_timed_out_then_suppressed_transform(home, monkeypatch, section, withheld):
    """A timeout, then the suppression window that skips the callback on later calls."""
    _write_config(home, section, timeout=0.2)
    _install(monkeypatch, _slow)
    first, second = _handle(), _handle()
    for result in (first, second):
        if withheld:
            _assert_withheld(result, "timed out or skipped")
        else:
            assert result == RAW


@pytest.mark.parametrize("section, withheld", [(OFF_SECTIONS[2], False), (ON_SECTIONS[0], True)])
def test_p6_the_transform_step_itself_raising(home, monkeypatch, section, withheld):
    _write_config(home, section)
    _install(monkeypatch, _redact)
    with patch("model_tools._tool_result_observer_fields", side_effect=RuntimeError("step broke")):
        result = _handle()
    if withheld:
        _assert_withheld(result, "RuntimeError")
    else:
        assert result == RAW


@pytest.mark.parametrize("section", OFF_SECTIONS[2:3] + ON_SECTIONS)
def test_a_working_redaction_is_unchanged(home, monkeypatch, section):
    _write_config(home, section)
    _install(monkeypatch, _redact)
    assert _handle() == REDACTED


def test_p6_withholding_wins_over_an_earlier_string_return(home, monkeypatch):
    """Upstream takes the first string return; a failed redaction must not lose to it."""
    _write_config(home, ON_SECTIONS[0])
    _install(monkeypatch, lambda result=None, **_: str(result) + " (annotated)", _raises)
    _assert_withheld(_handle(), "RuntimeError")


def test_p6_on_without_a_transform_hook_changes_nothing(home, monkeypatch):
    _write_config(home, ON_SECTIONS[0])
    _install(monkeypatch)
    assert _handle() == RAW
    assert ht_patches.withhold_on_transform_error("web_search", RuntimeError("x")) is None


# --- the agent loops and AIAgent._invoke_tool ------------------------------------------------------

def _run_loop(mode: str) -> str:
    agent = _make_agent("web_search")
    tc = _mock_tool_call("web_search", json.dumps({"query": "q"}), "c-p6")
    messages = []
    with patch("model_tools.registry.dispatch", return_value=RAW):
        getattr(agent, f"_execute_tool_calls_{mode}")(SimpleNamespace(content="", tool_calls=[tc]),
                                                      messages, "task-1")
    return messages[0]["content"]


def _invoke() -> str:
    agent = _make_agent("web_search")
    with patch("model_tools.registry.dispatch", return_value=RAW):
        return agent._invoke_tool("web_search", {"query": "q"}, "task-1", tool_call_id="c-p6")


@pytest.mark.parametrize("path", ["sequential", "concurrent", "invoke_tool"])
@pytest.mark.parametrize("section, withheld", [(OFF_SECTIONS[0], False), (OFF_SECTIONS[2], False),
                                               (ON_SECTIONS[0], True), (ON_SECTIONS[1], True)])
def test_p6_agent_paths(home, monkeypatch, path, section, withheld):
    _write_config(home, section)
    _install(monkeypatch, _raises)
    text = _invoke() if path == "invoke_tool" else _run_loop(path)
    if withheld:
        _assert_withheld(text, "RuntimeError")
    else:
        assert SECRET in text  # upstream: the transform was skipped


# --- the inline-executor helper (agent/inline_tool_executors.py) ------------------------------------

@pytest.mark.parametrize("section, withheld", [(OFF_SECTIONS[2], False), (ON_SECTIONS[0], True)])
def test_p6_inline_executor_helper(home, monkeypatch, section, withheld):
    from agent.inline_tool_executors import apply_transform_tool_result
    _write_config(home, section)
    _install(monkeypatch, _redact)
    with patch("model_tools._apply_transform_tool_result_hook", side_effect=RuntimeError("x")):
        result = apply_transform_tool_result(SimpleNamespace(session_id="s1"), function_name="web_search",
                                             function_args={}, result=RAW, effective_task_id="t1",
                                             tool_call_id="c1")
    if withheld:
        _assert_withheld(result, "RuntimeError")
    else:
        assert result == RAW


# --- the async dispatcher and the flag ---------------------------------------------------------------

@pytest.mark.parametrize("section, withheld", [(OFF_SECTIONS[2], False), (ON_SECTIONS[0], True)])
def test_p6_async_dispatch(home, monkeypatch, section, withheld):
    _write_config(home, section)
    mgr = _install(monkeypatch, _raises)
    results = asyncio.run(mgr.ainvoke_hook("transform_tool_result", tool_name="web_search", result=RAW))
    assert bool(results) is withheld
    if withheld:
        _assert_withheld(results[0], "RuntimeError")


def test_p6_other_hooks_are_untouched(home, monkeypatch):
    _write_config(home, ON_SECTIONS[0])
    import hermes_cli.plugins as plugins_mod
    mgr = PluginManager()
    mgr._discovered = True
    mgr._hooks["post_tool_call"] = [_raises]
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    assert mgr.invoke_hook("post_tool_call", tool_name="web_search", result=RAW) == []


def test_p6_an_unreadable_config_fails_closed(monkeypatch):
    def broken(*_a, **_kw):
        raise OSError("config unreadable")
    monkeypatch.setattr(ht_patches, "enabled_patches", broken)
    withheld = ht_patches.withhold_on_transform_failure("transform_tool_result", {"tool_name": "web_search"},
                                                        "RuntimeError")
    assert isinstance(withheld, ht_patches.ResultWithheld)
    assert withheld.error_type == ht_patches.HOOK_DISPATCH_ERROR


@pytest.mark.parametrize("section, refused", [({FLAG: True}, False), ({FLAG: "true"}, True),
                                              ({"p6_tool_result_transform_fail_close": True}, True)])
def test_p6_flag_is_checked_like_the_others(home, section, refused):
    _write_config(home, section)
    if refused:
        with pytest.raises(ht_patches.UnsupportedPatchCombination):
            ht_patches.check_patch_combination()
    else:
        assert ht_patches.check_patch_combination() == frozenset({ht_patches.P6})
