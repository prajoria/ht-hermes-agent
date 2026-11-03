"""HT fork patch P1 (HT-PATCHES.md): run identity in plugin tool-hook kwargs, behind
``ht_patches.p1_run_hook_kwargs`` (default off), and the startup check on patch combinations.

Each combination in ``SUPPORTED_COMBINATIONS`` is exercised here with the flag both off and on.
"""

import json
import os
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import ht_patches
from hermes_cli.plugins import PluginManager

P1_KWARGS = {"run_id", "gateway_session_key", "run_metadata", "pre_modify_args"}
KEY = "job-1.a1:htc_0123456789abcdefghijkl"


def _write_config(home, section):
    (home / "config.yaml").write_text(json.dumps({"ht_patches": section} if section is not None else {}))


@pytest.fixture()
def home():
    """The per-test HERMES_HOME the autouse isolation fixture already set up."""
    return Path(os.environ["HERMES_HOME"])


@pytest.fixture()
def hooks(monkeypatch):
    """A plugin manager whose pre_tool_call injects ``secret`` via modify; records every payload."""
    import hermes_cli.plugins as plugins_mod

    seen: dict[str, list[dict]] = defaultdict(list)

    def pre(**kw):
        seen["pre_tool_call"].append(dict(kw))
        return {"action": "modify", "args": {"secret": "injected"}}

    mgr = PluginManager()
    mgr._discovered = True
    mgr._hooks["pre_tool_call"] = [pre]
    for name in ("post_tool_call", "transform_tool_result", "on_session_start"):
        mgr._hooks[name] = [lambda _n=name, **kw: seen[_n].append(dict(kw))]
    monkeypatch.setattr(plugins_mod, "_plugin_manager", mgr)
    return seen


def _call_tool(dispatched):
    registry = MagicMock()
    registry.dispatch.side_effect = lambda name, args, **kw: dispatched.append(dict(args)) or json.dumps({"ok": 1})
    with patch("model_tools.registry", registry):
        from model_tools import handle_function_call
        handle_function_call("web_search", {"query": "q"}, task_id="t1", session_id="s1", tool_call_id="call-1")


@pytest.mark.parametrize("section", [None, {}, {"p1_run_hook_kwargs": False}, {"p1_run_hook_kwargs": "true"}])
def test_p1_defaults_off(home, hooks, section):
    _write_config(home, section)
    assert ht_patches.check_patch_combination() == frozenset()
    dispatched = []
    with ht_patches.run_hook_scope("run_abc", KEY):
        _call_tool(dispatched)
    assert dispatched == [{"query": "q", "secret": "injected"}]  # upstream behaviour unchanged
    for name in ht_patches.TOOL_HOOKS:
        assert hooks[name] and not P1_KWARGS & set(hooks[name][0]), name


def test_p1_on_tool_hooks_receive_run_identity(home, hooks):
    _write_config(home, {"p1_run_hook_kwargs": True})
    assert ht_patches.check_patch_combination() == frozenset({ht_patches.P1})
    dispatched = []
    with ht_patches.run_hook_scope("run_abc", KEY):
        _call_tool(dispatched)
        from hermes_cli.plugins import invoke_hook
        invoke_hook("on_session_start", session_id="s1")  # not a tool hook
    assert dispatched == [{"query": "q", "secret": "injected"}]
    for name in ht_patches.TOOL_HOOKS:
        (kw,) = hooks[name]
        assert kw["run_id"] == "run_abc" and kw["gateway_session_key"] == KEY, name
        assert kw["run_metadata"] == {"platform": "api_server", "endpoint": "/v1/runs"}, name
        assert kw["task_id"] == "t1" and kw["session_id"] == "s1", name  # upstream kwargs kept
    post = hooks["post_tool_call"][0]
    assert post["args"] == {"query": "q", "secret": "injected"}  # upstream: the modified args
    assert post["pre_modify_args"] == {"query": "q"}  # P1: what the model sent
    assert "pre_modify_args" not in hooks["pre_tool_call"][0]
    assert not P1_KWARGS & set(hooks["on_session_start"][0])


def test_p1_on_without_a_bound_run_adds_nothing(home, hooks):
    _write_config(home, {"p1_run_hook_kwargs": True})
    _call_tool([])
    assert not P1_KWARGS & set(hooks["pre_tool_call"][0])
    assert not P1_KWARGS & set(hooks["post_tool_call"][0])


def test_p1_on_without_a_declared_key_sends_an_empty_key(home, hooks):
    _write_config(home, {"p1_run_hook_kwargs": True})
    with ht_patches.run_hook_scope("run_abc", None):
        _call_tool([])
    assert hooks["pre_tool_call"][0]["gateway_session_key"] == ""
    assert hooks["pre_tool_call"][0]["run_id"] == "run_abc"


def test_p1_session_key_never_reaches_shell_hooks_or_webhooks(home):
    from agent.shell_hooks import _payload_fields
    fields = _payload_fields({"tool_name": "web_search", "args": {}, "session_id": "s1", "run_id": "run_abc",
                              "gateway_session_key": KEY})
    assert fields["extra"]["run_id"] == "run_abc"
    assert "gateway_session_key" not in fields["extra"]
    assert KEY not in json.dumps(fields)


@pytest.mark.parametrize("section, message", [
    ({"p1_run_hook_kwargs": True, "p9_unknown": True}, "unknown ht_patches flag"),
    ({"p1_run_hook_kwrags": True}, "unknown ht_patches flag"),
])
def test_unknown_flag_is_rejected(home, section, message):
    _write_config(home, section)
    with pytest.raises(ht_patches.UnsupportedPatchCombination, match=message):
        ht_patches.check_patch_combination()


def test_untested_combination_is_rejected(home, monkeypatch):
    """A combination missing from SUPPORTED_COMBINATIONS (here: P1 alone) is refused."""
    monkeypatch.setattr(ht_patches, "SUPPORTED_COMBINATIONS", frozenset({frozenset()}))
    _write_config(home, {"p1_run_hook_kwargs": True})
    with pytest.raises(ht_patches.UnsupportedPatchCombination, match="untested ht_patches combination"):
        ht_patches.check_patch_combination()


@pytest.mark.parametrize("section, refused", [({}, False), ({"p1_run_hook_kwargs": True}, False),
                                              ({"p1_typo": True}, True)])
def test_gateway_startup_refuses_an_unsupported_combination(home, section, refused):
    from gateway.run import GatewayRunner
    _write_config(home, section)
    runner = MagicMock()
    assert GatewayRunner._start_check_ht_patches(runner) is refused
    assert runner._startup_fail_fatal_config.called is refused
