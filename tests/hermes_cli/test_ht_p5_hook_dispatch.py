"""HT fork patch P5 (HT-PATCHES.md): a ``pre_tool_call`` hook dispatcher that raises fails closed,
behind ``ht_patches.p5_hook_dispatch_fail_closed`` (default off).

Upstream swallows the dispatcher's own error and runs the call without the hooks' verdict. Each
dispatch path is exercised with the flag off (upstream: the tool runs) and on (refused, typed
``plugin_hook_error``): ``model_tools.handle_function_call``, the agent's sequential and concurrent
loops (``agent/tool_executor.py``) and ``AIAgent._invoke_tool`` (``agent/agent_runtime_helpers.py``).
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import ht_patches
from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call

DISPATCHER = "hermes_cli.plugins._dispatch_pre_tool_call_hooks"
# The error text may quote tool arguments; P5 must not echo it.
BOOM = RuntimeError("dispatcher broke near secret-arg-value")
OFF_SECTIONS = [None, {}, {"p5_hook_dispatch_fail_closed": False}, {"p1_run_hook_kwargs": True}]
ON_SECTIONS = [{"p5_hook_dispatch_fail_closed": True},
               {"p1_run_hook_kwargs": True, "p5_hook_dispatch_fail_closed": True}]


@pytest.fixture()
def home():
    return Path(os.environ["HERMES_HOME"])


def _write_config(home, section):
    (home / "config.yaml").write_text(json.dumps({"ht_patches": section} if section is not None else {}))


def _assert_refused(text: str):
    """``text`` is the result the model sees (the agent loop wraps it as untrusted tool output)."""
    assert ('{"error": "BLOCKED: the pre_tool_call hook dispatcher failed for web_search (RuntimeError); '
            'the call was refused"}') in text
    assert "secret-arg-value" not in text


# --- model_tools.handle_function_call (_pre_dispatch_guards) ---------------------------------------

def _handle(dispatched, post):
    registry = MagicMock()
    registry.dispatch.side_effect = lambda name, args, **kw: dispatched.append(dict(args)) or json.dumps({"ok": 1})
    with patch("model_tools.registry", registry), patch(DISPATCHER, side_effect=BOOM), \
            patch("model_tools._emit_post_tool_call_hook", side_effect=lambda **kw: post.append(kw)):
        from model_tools import handle_function_call
        return handle_function_call("web_search", {"query": "q"}, task_id="t1", session_id="s1",
                                    tool_call_id="call-1")


@pytest.mark.parametrize("section", OFF_SECTIONS)
def test_p5_off_handle_function_call_proceeds_as_upstream(home, section):
    _write_config(home, section)
    dispatched, post = [], []
    result = _handle(dispatched, post)
    assert dispatched == [{"query": "q"}]
    assert json.loads(result) == {"ok": 1}


@pytest.mark.parametrize("section", ON_SECTIONS)
def test_p5_on_handle_function_call_fails_closed(home, section):
    _write_config(home, section)
    dispatched, post = [], []
    result = _handle(dispatched, post)
    assert dispatched == []
    _assert_refused(result)
    assert post and post[-1]["status"] == "blocked"
    assert post[-1]["error_type"] == ht_patches.HOOK_DISPATCH_ERROR


# --- agent loops (agent/tool_executor.py) ----------------------------------------------------------

def _run_loop(mode: str):
    agent = _make_agent("web_search")
    tc = _mock_tool_call("web_search", json.dumps({"query": "q"}), "c-p5")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []
    post = []
    with patch(DISPATCHER, side_effect=BOOM), \
            patch("model_tools.handle_function_call", return_value=json.dumps({"ran": True})) as hfc, \
            patch("agent.tool_executor._ToolCallRef.emit_post",
                  side_effect=lambda _agent, _result, **kw: post.append(kw), autospec=False):
        getattr(agent, f"_execute_tool_calls_{mode}")(msg, messages, "task-1")
    return hfc, messages, post


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
@pytest.mark.parametrize("section", OFF_SECTIONS)
def test_p5_off_agent_loop_proceeds_as_upstream(home, section, mode):
    _write_config(home, section)
    hfc, messages, _post = _run_loop(mode)
    hfc.assert_called_once()
    assert json.loads(messages[0]["content"]) == {"ran": True}


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
@pytest.mark.parametrize("section", ON_SECTIONS)
def test_p5_on_agent_loop_fails_closed(home, section, mode):
    _write_config(home, section)
    hfc, messages, post = _run_loop(mode)
    hfc.assert_not_called()
    _assert_refused(messages[0]["content"])
    assert post and post[-1]["status"] == "blocked"
    assert post[-1]["error_type"] == ht_patches.HOOK_DISPATCH_ERROR


# --- AIAgent._invoke_tool (agent/agent_runtime_helpers.py) -----------------------------------------

def _invoke():
    agent = _make_agent("web_search")
    post = []
    with patch(DISPATCHER, side_effect=BOOM), \
            patch("model_tools.handle_function_call", return_value=json.dumps({"ran": True})) as hfc, \
            patch("agent.inline_tool_executors.emit_terminal_post_tool_call",
                  side_effect=lambda *_a, **kw: post.append(kw)):
        result = agent._invoke_tool("web_search", {"query": "q"}, "task-1", tool_call_id="c-p5")
    return hfc, result, post


@pytest.mark.parametrize("section", OFF_SECTIONS)
def test_p5_off_invoke_tool_proceeds_as_upstream(home, section):
    _write_config(home, section)
    hfc, result, _post = _invoke()
    hfc.assert_called_once()
    assert "ran" in result


@pytest.mark.parametrize("section", ON_SECTIONS)
def test_p5_on_invoke_tool_fails_closed(home, section):
    _write_config(home, section)
    hfc, result, post = _invoke()
    hfc.assert_not_called()
    _assert_refused(result)
    assert post and post[-1]["status"] == "blocked"
    assert post[-1]["error_type"] == ht_patches.HOOK_DISPATCH_ERROR


# --- the flag itself -------------------------------------------------------------------------------

def test_p5_a_plugin_block_keeps_its_own_type(home):
    """P5 changes only the dispatcher-error path: a plugin's veto is still ``plugin_block``."""
    _write_config(home, {"p5_hook_dispatch_fail_closed": True})
    agent = _make_agent("web_search")
    tc = _mock_tool_call("web_search", json.dumps({"query": "q"}), "c-p5")
    post = []
    with patch(DISPATCHER, return_value=("plugin policy", None)), \
            patch("model_tools.handle_function_call", return_value="SHOULD_NOT_RUN") as hfc, \
            patch("agent.tool_executor._ToolCallRef.emit_post",
                  side_effect=lambda _agent, _result, **kw: post.append(kw)):
        agent._execute_tool_calls_sequential(SimpleNamespace(content="", tool_calls=[tc]), [], "task-1")
    hfc.assert_not_called()
    assert post[-1]["error_type"] == "plugin_block"


def test_p5_an_unreadable_config_fails_closed(monkeypatch):
    def broken(*_a, **_kw):
        raise OSError("config unreadable")
    monkeypatch.setattr(ht_patches, "enabled_patches", broken)
    refusal = ht_patches.refuse_on_hook_dispatch_error("web_search", BOOM)
    assert isinstance(refusal, ht_patches.HookDispatchRefusal)
    assert refusal.error_type == ht_patches.HOOK_DISPATCH_ERROR


@pytest.mark.parametrize("section, refused", [({"p5_hook_dispatch_fail_closed": True}, False),
                                              ({"p5_hook_dispatch_fail_closed": "true"}, True),
                                              ({"p5_hook_dispatch_fail_close": True}, True)])
def test_p5_flag_is_checked_like_the_others(home, section, refused):
    _write_config(home, section)
    if refused:
        with pytest.raises(ht_patches.UnsupportedPatchCombination):
            ht_patches.check_patch_combination()
    else:
        assert ht_patches.check_patch_combination() == frozenset({ht_patches.P5})
