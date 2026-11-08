"""HT fork patches (register: ``HT-PATCHES.md`` at the repo root).

Every patch is additive and off unless ``config.yaml`` turns it on under ``ht_patches:``. Only the
flag combinations in ``SUPPORTED_COMBINATIONS`` are tested; the gateway refuses to start on any
other (``check_patch_combination``).

P1 (``ht_patches.p1_run_hook_kwargs``): an API-server ``/v1/runs`` turn binds its run identity for
the turn, and the plugin tool hooks (``pre_tool_call``, ``post_tool_call``,
``transform_tool_result``) receive it as three extra kwargs:

- ``run_id``: the ``/v1/runs`` run id.
- ``gateway_session_key``: the caller's ``X-Hermes-Session-Key`` (``""`` when none was sent).
  Under the session authority the agent itself only sees the route key, so the hook gets it here.
- ``run_metadata``: a dict about the run (``platform``, ``endpoint``).

``post_tool_call`` also receives ``pre_modify_args``: the arguments as they were before any
``pre_tool_call`` ``modify`` directive, so an observer need not see values a hook injected.
First-party observers never receive these kwargs; only plugin hook callbacks do.

P4 (``ht_patches.p4_mcp_private_ca``): an HTTP/SSE MCP server configured with ``ssl_verify: <CA
path>`` and a client certificate gets one ``ssl.SSLContext`` trusting that CA with the client
certificate loaded (``tools.mcp_tool_transport._tls_verify``), so mTLS behind a private CA works.

P5 (``ht_patches.p5_hook_dispatch_fail_closed``): when the ``pre_tool_call`` hook dispatcher itself
raises (not a plugin callback: upstream already blocks on those), the tool call is refused with a
``plugin_hook_error`` block instead of running unguarded (``refuse_on_hook_dispatch_error``).

P6 (``ht_patches.p6_tool_result_transform_fail_closed``): when a ``transform_tool_result`` callback
raises, times out or is skipped (still running, suppressed after a timeout, abandoned-worker cap),
or the transform step itself raises, the tool result is withheld instead of reaching the model
untransformed (``withhold_on_transform_failure``, ``withhold_on_transform_error``,
``pick_transform_result``). A transform that redacts (the HLM Harness plugin strips the job
capability there) must not be skipped silently.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Iterator, Mapping, Optional

P1 = "P1"
P4 = "P4"
P5 = "P5"
P6 = "P6"
# Patch id -> its flag under ``ht_patches:`` in config.yaml.
PATCH_FLAGS = {
    P1: "p1_run_hook_kwargs",
    P4: "p4_mcp_private_ca",
    P5: "p5_hook_dispatch_fail_closed",
    P6: "p6_tool_result_transform_fail_closed",
}
# Each combination is exercised by tests/hermes_cli/test_ht_patches.py (and the P1 run tests and
# tests/tools/test_mcp_client_private_ca.py for P4, tests/hermes_cli/test_ht_p5_hook_dispatch.py
# for P5, tests/hermes_cli/test_ht_p6_transform_fail_closed.py for P6). P5 touches only the
# pre_tool_call error path and P6 only the transform_tool_result failure path, so each is tested
# with each of the others.
SUPPORTED_COMBINATIONS = frozenset(
    base | p5 | p6
    for base in (frozenset(), frozenset({P1}), frozenset({P4}), frozenset({P1, P4}))
    for p5 in (frozenset(), frozenset({P5}))
    for p6 in (frozenset(), frozenset({P6}))
)

TOOL_HOOKS = frozenset({"pre_tool_call", "post_tool_call", "transform_tool_result"})
# Every kwarg P1 adds to a tool-hook payload (``pre_modify_args`` on ``post_tool_call`` only).
P1_HOOK_KWARGS = frozenset({"run_id", "gateway_session_key", "run_metadata", "pre_modify_args"})
# Kwargs that carry a caller credential: never forwarded to shell hooks or outbound webhooks.
PRIVATE_HOOK_KWARGS = frozenset({"gateway_session_key"})


logger = logging.getLogger(__name__)


class UnsupportedPatchCombination(RuntimeError):
    """The enabled HT patches are not a tested combination."""


def _patch_config(config: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    section = config.get("ht_patches")
    return section if isinstance(section, Mapping) else {}


def _enabled(section: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(patch for patch, flag in PATCH_FLAGS.items() if section.get(flag) is True)


def enabled_patches(config: Optional[Mapping[str, Any]] = None) -> frozenset[str]:
    """Patch ids whose flag is literally ``true``."""
    return _enabled(_patch_config(config))


def check_patch_combination(config: Optional[Mapping[str, Any]] = None) -> frozenset[str]:
    """Return the enabled patches, or raise when the flags are not a tested combination.

    An unknown flag, or a value that is not a YAML boolean (``"true"`` quoted), is refused too: a
    typo must not silently run a pin without the patch the deployment asked for."""
    section = _patch_config(config)
    unknown = sorted(set(section) - set(PATCH_FLAGS.values()))
    if unknown:
        raise UnsupportedPatchCombination(f"unknown ht_patches flag(s): {', '.join(map(str, unknown))}")
    not_bool = sorted(str(flag) for flag, value in section.items() if not isinstance(value, bool))
    if not_bool:
        raise UnsupportedPatchCombination(f"ht_patches flag(s) not true/false: {', '.join(not_bool)}")
    enabled = _enabled(section)
    if enabled not in SUPPORTED_COMBINATIONS:
        raise UnsupportedPatchCombination(
            f"untested ht_patches combination: {sorted(enabled)}; supported: "
            f"{sorted(sorted(c) for c in SUPPORTED_COMBINATIONS)}")
    return enabled


RUN_METADATA = {"platform": "api_server", "endpoint": "/v1/runs"}
_run_identity: ContextVar[Optional[dict[str, Any]]] = ContextVar("ht_p1_run_identity", default=None)


@contextmanager
def _bind(run_id: str, gateway_session_key: Optional[str]) -> Iterator[None]:
    identity = {
        "run_id": run_id,
        "gateway_session_key": gateway_session_key or "",
        # (task_id, tool_call_id) -> args seen by pre_tool_call, consumed by post_tool_call
        "pre_modify_args": {},
    }
    token = _run_identity.set(identity)
    try:
        yield
    finally:
        _run_identity.reset(token)
        # Contexts copied inside the scope (review forks, async delegation, deferred work) share
        # this dict: emptying it ends the run's identity, and drops the key, in every copy.
        identity.clear()


def run_hook_scope(run_id: str, gateway_session_key: Optional[str]):
    """Bind a ``/v1/runs`` turn's identity for the tool hooks it fires (no-op while P1 is off).

    Enter it on the thread (or in the context copied to it) that runs the agent."""
    if P1 not in enabled_patches():
        return nullcontext()
    return _bind(run_id, gateway_session_key)


@contextmanager
def remember_run_key(adapter: Any, run_id: str, gateway_session_key: Optional[str]) -> Iterator[None]:
    """Hold a run's declared session key in memory for the session-authority drain (P1 on only),
    from before its admission (the drain may start during it).

    It is never written to the admission row: the key can carry a capability handle. The run's
    ``_forget_run`` tables include ``_ht_run_hook_keys``, so the entry goes with the run; an
    admission that raises or is cancelled drops it here, since no run task will retire it."""
    if P1 in enabled_patches():
        adapter._ht_run_hook_keys[run_id] = gateway_session_key or ""
    try:
        yield
    except BaseException:
        adapter._ht_run_hook_keys.pop(run_id, None)
        raise


def admitted_run_scope(adapter: Any, request_id: str):
    """The drain's ``run_hook_scope`` for an admitted ``/v1/runs`` turn, whose admission request
    id is its run id; a no-op for every other admission. A key is held only when P1 was on at
    admission, so the flag is not read again here."""
    key = adapter._ht_run_hook_keys.get(request_id)
    return nullcontext() if key is None else _bind(request_id, key)


def add_run_hook_kwargs(hook_name: str, kwargs: dict[str, Any]) -> None:
    """Add P1's kwargs to a plugin tool-hook payload when a run identity is bound."""
    identity = _run_identity.get()
    if not identity or hook_name not in TOOL_HOOKS:  # unbound, or its scope has exited
        return
    kwargs["run_id"] = identity["run_id"]
    kwargs["gateway_session_key"] = identity["gateway_session_key"]
    kwargs["run_metadata"] = dict(RUN_METADATA)
    call_id = kwargs.get("tool_call_id")
    if not call_id:
        return
    # The root would be passing pre-modify args through model_tools/tool_executor; this side table
    # keeps the patch out of those upstream files. Entries die with the run's context.
    # Keyed with task_id too: copies of the context (subagents) share the table, and model-chosen
    # call ids are not unique across agents.
    pending, slot = identity["pre_modify_args"], (kwargs.get("task_id"), call_id)
    if hook_name == "pre_tool_call":
        args = kwargs.get("args")
        pending[slot] = dict(args) if isinstance(args, dict) else {}
    elif hook_name == "post_tool_call":
        kwargs["pre_modify_args"] = pending.pop(slot, kwargs.get("args"))


# P5: the error type a refused call carries (the post_tool_call ``error_type`` and the block body).
HOOK_DISPATCH_ERROR = "plugin_hook_error"


class HookDispatchRefusal(str):
    """P5's block message; ``error_type`` tells a caller the refusal from a plugin's own block."""

    error_type = HOOK_DISPATCH_ERROR


def refuse_on_hook_dispatch_error(tool_name: str, error: BaseException) -> Optional[HookDispatchRefusal]:
    """The block for a ``pre_tool_call`` dispatcher that raised, or ``None`` (upstream: the call
    proceeds without the hooks' verdict) while P5 is off.

    A config that cannot be read counts as P5 on: the guard did not run, so the call does not
    either. The message and the log name the error's type only, never its text, which may quote
    the tool arguments."""
    try:
        on = P5 in enabled_patches()
    except Exception:  # noqa: BLE001 -- unreadable config: fail closed
        on = True
    if not on:
        return None
    logger.warning("pre_tool_call hook dispatch failed for %s (%s); call refused (HT patch P5)",
                   tool_name, type(error).__name__)
    return HookDispatchRefusal(
        f"BLOCKED: the pre_tool_call hook dispatcher failed for {tool_name} ({type(error).__name__}); "
        "the call was refused")


# P6: the hook whose failures withhold the result, and the withheld result.
TRANSFORM_HOOK = "transform_tool_result"


class ResultWithheld(str):
    """P6's replacement result (a JSON ``{"error": ...}`` string); it wins over any other
    ``transform_tool_result`` return."""

    error_type = HOOK_DISPATCH_ERROR


def _p6_on() -> bool:
    try:
        return P6 in enabled_patches()
    except Exception:  # noqa: BLE001 -- unreadable config: fail closed
        return True


def _withhold(tool_name: Any, reason: str) -> ResultWithheld:
    logger.warning("transform_tool_result failed for %s (%s); result withheld (HT patch P6)",
                   tool_name, reason)
    message = (f"BLOCKED: the transform_tool_result hook failed for {tool_name} ({reason}); "
               "the result was withheld")
    return ResultWithheld(json.dumps({"error": message}, ensure_ascii=False))


def withhold_on_transform_failure(hook_name: str, kwargs: Mapping[str, Any], reason: str
                                  ) -> Optional[ResultWithheld]:
    """P6, per callback: the withheld result for a ``transform_tool_result`` callback that raised
    (``reason`` = the error's type) or timed out / was skipped; ``None`` for any other hook or
    while P6 is off (upstream: the callback is skipped and the result passes untransformed)."""
    if hook_name != TRANSFORM_HOOK or not _p6_on():
        return None
    return _withhold(kwargs.get("tool_name"), reason)


def withhold_on_transform_error(tool_name: str, error: BaseException) -> Optional[ResultWithheld]:
    """P6, the transform step itself raised: the withheld result, or ``None`` while P6 is off or
    no ``transform_tool_result`` callback is registered (nothing would have transformed it)."""
    if not _p6_on():
        return None
    try:
        from hermes_cli.lifecycle import has_hook
        if not has_hook(TRANSFORM_HOOK):
            return None
    except Exception:  # noqa: BLE001 -- cannot tell: a transform may be registered
        pass
    return _withhold(tool_name, type(error).__name__)


def pick_transform_result(hook_results: Any, result: Any) -> Any:
    """The result after ``transform_tool_result``: a P6 withheld result if any callback failed,
    else upstream's first string return, else ``result``."""
    strings = [r for r in hook_results if isinstance(r, str)]
    withheld = [r for r in strings if isinstance(r, ResultWithheld)]
    return withheld[0] if withheld else (strings[0] if strings else result)
