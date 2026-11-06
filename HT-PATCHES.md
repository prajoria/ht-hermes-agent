# HT patches

The HumanTronik patches this fork (`prajoria/ht-hermes-agent`) carries on top of upstream
`NousResearch/hermes-agent`. The register is design §15 of the HLM Harness design
(`HumanTronik/HT-HLM` `docs/ai/Harness/hermes-workflow-engine-design.md`).

Rules for every patch:

- One PR per patch, every commit subject prefixed `ht:`. It lands on the fork's integration
  branch `prajoria/ht-hlm-agent` by fast-forward or merge commit, never squash, so the commit an
  HT-HLM bump pins stays on the branch; it reaches `hlm/main` at release. `main` mirrors upstream.
- Additive only, and **off by default** behind a flag under `ht_patches:` in `config.yaml`.
- Tested with its flag off and on. Every supported combination of flags is listed in
  `SUPPORTED_COMBINATIONS` (`hermes_cli/ht_patches.py`) and tested; the gateway refuses to start
  (fatal config, exit 78) on any other combination, on an unknown `ht_patches` flag, or on a flag
  value that is not a YAML boolean (a quoted `"true"` would otherwise mean off).
- Written the way an upstream contribution would be. Nothing is sent upstream without the owner's
  decision.
- Each entry names its removal criterion.

## Supported flag combinations

| Combination | Tests |
| --- | --- |
| none (upstream behaviour) | `tests/hermes_cli/test_ht_patches.py`, `tests/gateway/test_ht_p1_run_hook_kwargs.py`, `tests/tools/test_mcp_client_private_ca.py` (flags unset and `false`) |
| P1 | the same files, P1 flag `true` |
| P4 | `tests/hermes_cli/test_ht_patches.py`, `tests/tools/test_mcp_client_private_ca.py`, P4 flag `true` |
| P1+P4 | the same files, both flags `true` |

P2 and P3 are reserved by HumanTronik/HT-HLM#304 (HTK010 fork patches P2 run metadata and P3
`on_write_staged` hook).

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
  `session_id` (design §7.2: the guard refuses an unknown `task_id`). The key is held in memory
  only, so a run the session authority drains after a gateway restart gets no P1 kwargs (the
  plugin sees an unbound turn and must fail closed). The identity ends with the run's scope:
  work that copied the run's context (a review fork, async delegation, deferred work) gets no
  P1 kwargs after the run ends, though turns the drain runs inside the scope (follow-ups in
  the same session) do.
- **Limits:** every plugin tool-hook callback that takes `**kwargs` receives
  `gateway_session_key`, not only `hlm_harness`; enable only trusted plugins where P1 is on.
  The flag is read from the active profile's config; in a multiplexed gateway set it in the
  managed scope (as the HLM image does), not per profile.
- **Removal criterion:** upstream passes the declared session key (or the run id and request
  metadata) to tool hooks on `/v1/runs` itself, or the HLM Harness plugin stops binding the job
  capability from the hook kwargs (it binds by `task_id` alone, design §7.2). Then drop the flag,
  this entry and `hermes_cli/ht_patches.py`'s P1 parts.

## P4: MCP client trusts a private CA and presents the client certificate

- **Issue:** HumanTronik/HT-HLM#641 (FU641 Hermes MCP client: trust a private CA and present the
  client certificate). Found by #586 (DEMO04); the fix was named by S4 (HTK005, HT-HLM#299).
- **Flag:** `ht_patches.p4_mcp_private_ca: true` (default off).
- **What it does:** for an HTTP or SSE MCP server configured with `ssl_verify: <CA path>` (a file
  or a directory) **and** a client certificate (`client_cert`, with or without `client_key`), the
  client builds one `ssl.SSLContext` trusting that CA, loads the client certificate chain into it
  and passes it to httpx as `verify=` with no `cert=`. Upstream passes the two separately: httpx
  0.28's `create_ssl_context` returns early for `verify=<str>` and drops `cert=`, and httpx2
  refuses the pair with a `TypeError`, so mTLS to a server behind a private CA (the HLM lab's Envoy
  proxy in front of Harness.Mcp) never presented the client certificate. The runtime's plugin
  seal refuses `SSL_CERT_FILE` and every other trust-store variable (HT-HLM#491), so the CA can
  only come from the server's own `ssl_verify`.
- **Where:** `_tls_verify` in `tools/mcp_tool_transport.py`, applied once per connect where the
  config is read: `_run_http` (Streamable HTTP, SSE, proxy mounts) and the content-type preflight
  in `tools/mcp_tool_server_run.py`. The transports themselves are unchanged.
- **Not covered:** `ssl_verify: true`/`false` and a CA path without a client certificate keep
  upstream's handling, flag on or off. The deprecated Streamable HTTP API (mcp < 1.24) never sends
  a client certificate. With the flag off the behaviour is upstream's, failure included.
- **Removal criterion:** upstream httpx or the MCP SDK accepts a CA path together with a client
  certificate, or upstream Hermes builds an `ssl.SSLContext` for MCP TLS itself. Then drop the
  flag, this entry and `_tls_verify`.
