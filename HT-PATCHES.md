# HT patches

The HumanTronik patches this fork (`prajoria/ht-hermes-agent`) carries on top of upstream
`NousResearch/hermes-agent`. The register is design §15 of the HLM Harness design
(`HumanTronik/HT-HLM` `docs/ai/Harness/hermes-workflow-engine-design.md`).

Rules for every patch:

- One commit, subject prefixed `ht:`. It lands on the fork's integration branch
  `prajoria/ht-hlm-agent` and reaches `hlm/main` at release. `main` mirrors upstream.
- Additive only, and **off by default** behind a flag under `ht_patches:` in `config.yaml`.
- Tested with its flag off and on. Every supported combination of flags is listed in
  `SUPPORTED_COMBINATIONS` (`hermes_cli/ht_patches.py`) and tested; the gateway refuses to start
  (fatal config, exit 78) on any other combination or on an unknown `ht_patches` flag.
- Written the way an upstream contribution would be. Nothing is sent upstream without the owner's
  decision.
- Each entry names its removal criterion.

## Supported flag combinations

| Combination | Tests |
| --- | --- |
| none (upstream behaviour) | `tests/hermes_cli/test_ht_patches.py`, `tests/gateway/test_ht_p1_run_hook_kwargs.py` (flag unset and `false`) |
| P1 | the same files, flag `true` |

## P1: run id, session key and run metadata in tool hook kwargs

- **Issue:** HumanTronik/HT-HLM#303 (HTK009 Fork patch P1: run_id, gateway_session_key and run
  metadata in tool hook kwargs). Spike evidence: S1 (HTK005, HT-HLM#299) and S2 (HTK004, HT-HLM#298).
- **Flag:** `ht_patches.p1_run_hook_kwargs: true` (default off).
- **What it does:** for a `POST /v1/runs` turn, with and without the session authority, the plugin
  tool hooks `pre_tool_call`, `post_tool_call` and `transform_tool_result` receive three extra kwargs:
  - `run_id`: the run id `POST /v1/runs` returned.
  - `gateway_session_key`: the caller's `X-Hermes-Session-Key` (`""` when none was sent). Upstream
    gives the agent this key on the direct path only; under the session authority the agent sees
    only its route key (`agent:main:api_server:dm:<session_id>`), so the key never reached a hook.
    The key is held in memory for the run (`APIServerAdapter._ht_run_hook_keys`) and is never
    written to the admission row.
  - `run_metadata`: `{"platform": "api_server", "endpoint": "/v1/runs"}`. P2 would add the request's
    own metadata here.

  `post_tool_call` also receives `pre_modify_args`: the arguments `pre_tool_call` saw, before any
  `modify` directive (S1 follow-up 2). `args` keeps upstream's meaning (the arguments the tool ran with).
- **Where:** `hermes_cli/ht_patches.py` (flag, combination check, run scope); one call in
  `PluginManager.invoke_hook`/`ainvoke_hook` (`hermes_cli/plugins_dispatch.py`), so first-party
  observers never get the kwargs; the run scope in `gateway/platforms/api_server_runs.py`
  (`_run_agent_sync`, `_handle_runs`) and `gateway/session_ingress.py` (`execute_admission`);
  the startup check in `gateway/run_startup.py`. `gateway_session_key` is dropped from shell-hook and
  outbound-webhook payloads (`agent/shell_hooks.py`).
- **Not covered:** other API doors (`/v1/chat/completions`, `/v1/responses`) and non-API platforms
  get no P1 kwargs. A subagent's tool calls run in a copy of the parent's context, so they carry
  the parent run's kwargs too; a plugin that binds a credential must still check `task_id` and
  `session_id` (design §7.2: the guard refuses an unknown `task_id`).
- **Removal criterion:** upstream passes the declared session key (or the run id and request
  metadata) to tool hooks on `/v1/runs` itself, or the HLM Harness plugin stops binding the job
  capability from the hook kwargs (it binds by `task_id` alone, design §7.2). Then drop the flag,
  this entry and `hermes_cli/ht_patches.py`'s P1 parts.
