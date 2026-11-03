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
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any, Iterator, Mapping, Optional

P1 = "P1"
# Patch id -> its flag under ``ht_patches:`` in config.yaml.
PATCH_FLAGS = {P1: "p1_run_hook_kwargs"}
# Each combination is exercised by tests/hermes_cli/test_ht_patches.py (and the P1 run tests).
SUPPORTED_COMBINATIONS = frozenset({frozenset(), frozenset({P1})})

TOOL_HOOKS = frozenset({"pre_tool_call", "post_tool_call", "transform_tool_result"})
# Every kwarg P1 adds to a tool-hook payload (``pre_modify_args`` on ``post_tool_call`` only).
P1_HOOK_KWARGS = frozenset({"run_id", "gateway_session_key", "run_metadata", "pre_modify_args"})
# Kwargs that carry a caller credential: never forwarded to shell hooks or outbound webhooks.
PRIVATE_HOOK_KWARGS = frozenset({"gateway_session_key"})


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
    token = _run_identity.set({
        "run_id": run_id,
        "gateway_session_key": gateway_session_key or "",
        "pre_modify_args": {},  # tool_call_id -> args seen by pre_tool_call, consumed by post_tool_call
    })
    try:
        yield
    finally:
        _run_identity.reset(token)


def run_hook_scope(run_id: str, gateway_session_key: Optional[str]):
    """Bind a ``/v1/runs`` turn's identity for the tool hooks it fires (no-op while P1 is off).

    Enter it on the thread (or in the context copied to it) that runs the agent."""
    if P1 not in enabled_patches():
        return nullcontext()
    return _bind(run_id, gateway_session_key)


def remember_run_key(adapter: Any, run_id: str, gateway_session_key: Optional[str]) -> None:
    """Hold a run's declared session key in memory for the session-authority drain (P1 on only).

    It is never written to the admission row: the key can carry a capability handle. The run's
    ``_forget_run`` tables include ``_ht_run_hook_keys``, so the entry goes with the run."""
    if P1 in enabled_patches():
        adapter._ht_run_hook_keys[run_id] = gateway_session_key or ""


def admitted_run_scope(adapter: Any, request_id: str):
    """The drain's ``run_hook_scope`` for an admitted ``/v1/runs`` turn, whose admission request
    id is its run id; a no-op for every other admission. A key is held only when P1 was on at
    admission, so the flag is not read again here."""
    key = adapter._ht_run_hook_keys.get(request_id)
    return nullcontext() if key is None else _bind(request_id, key)


def add_run_hook_kwargs(hook_name: str, kwargs: dict[str, Any]) -> None:
    """Add P1's kwargs to a plugin tool-hook payload when a run identity is bound."""
    identity = _run_identity.get()
    if identity is None or hook_name not in TOOL_HOOKS:
        return
    kwargs["run_id"] = identity["run_id"]
    kwargs["gateway_session_key"] = identity["gateway_session_key"]
    kwargs["run_metadata"] = dict(RUN_METADATA)
    call_id = kwargs.get("tool_call_id")
    if not call_id:
        return
    # The root would be passing pre-modify args through model_tools/tool_executor; this side table
    # keeps the patch out of those upstream files. Entries die with the run's context.
    pending = identity["pre_modify_args"]
    if hook_name == "pre_tool_call":
        args = kwargs.get("args")
        pending[call_id] = dict(args) if isinstance(args, dict) else {}
    elif hook_name == "post_tool_call":
        kwargs["pre_modify_args"] = pending.pop(call_id, kwargs.get("args"))
