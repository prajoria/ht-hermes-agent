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
| P5, P1+P5, P4+P5, P1+P4+P5 | `tests/hermes_cli/test_ht_patches.py`, `tests/hermes_cli/test_ht_p5_hook_dispatch.py` (P5 flag `true`, alone and with P1); P5 changes only the `pre_tool_call` dispatcher-error path, which P1 and P4 do not touch |
| each of the above with P6 | `tests/hermes_cli/test_ht_patches.py`, `tests/hermes_cli/test_ht_p6_transform_fail_closed.py` (P6 flag `true`, alone and with P1+P5); P6 changes only the `transform_tool_result` failure path |

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
  and passes it to httpx as `verify=` with no `cert=`. The context trusts **only** the configured
  CA: no system or certifi CAs are loaded, on purpose, as with httpx's own `verify=<path>`. Upstream passes the two separately: httpx
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
- **Limits:** the flag is read from the active profile's config; in a multiplexed gateway set it
  in the managed scope (as the HLM image does for P1), not per profile.
- **Removal criterion:** upstream httpx or the MCP SDK accepts a CA path together with a client
  certificate, or upstream Hermes builds an `ssl.SSLContext` for MCP TLS itself. Then drop the
  flag, this entry and `_tls_verify`.

## P5: a `pre_tool_call` hook dispatcher error fails closed

- **Issue:** HumanTronik/HT-HLM#698 (FU698 Guard hardening: forbid profile shell hooks that would
  receive the job capability; dispatcher errors should fail closed). Found by the review of
  HT-HLM#694 (HTK031 Guard, job-token injection and redaction); owner decision 2026-11-07.
- **Flag:** `ht_patches.p5_hook_dispatch_fail_closed: true` (default off).
- **What it does:** when the `pre_tool_call` hook dispatcher itself raises
  (`hermes_cli.plugins._dispatch_pre_tool_call_hooks`: an import, the directive merge, the approval
  plumbing), upstream logs the error and runs the tool without the hooks' verdict. With P5 on, the
  call is refused instead: the model gets a `{"error": "BLOCKED: the pre_tool_call hook dispatcher
  failed for <tool> (<ErrorType>); the call was refused"}` result and the terminal `post_tool_call`
  carries `status: "blocked"` and `error_type: "plugin_hook_error"`. The message and the warning log
  name the error's type only, never its text (it may quote the arguments). A config that cannot be
  read while handling the error counts as P5 on.
- **Where:** `refuse_on_hook_dispatch_error` and `HookDispatchRefusal` in `hermes_cli/ht_patches.py`;
  one call in each place that swallows the dispatcher's error: `_pre_dispatch_guards`
  (`model_tools.py`), `_pre_tool_block` and its caller's `block_error_type`
  (`agent/tool_executor.py`, the agent's sequential and concurrent loops) and
  `_pre_tool_block_message` and `invoke_tool` (`agent/agent_runtime_helpers.py`).
- **Not covered:** a plugin callback that raises or times out is already blocked upstream
  (`_policy_error_block_directive`), flag on or off. Other hooks (`post_tool_call`,
  `transform_tool_result`, middleware) keep upstream's fail-open handling. With the flag off the
  behaviour is upstream's.
- **Limits:** the flag is read from the active profile's config on the error path; in a
  multiplexed gateway set it in the managed scope (as the HLM image does), not per profile.
- **Removal criterion:** upstream makes a `pre_tool_call` dispatcher error fail closed itself. Then
  drop the flag, this entry and `refuse_on_hook_dispatch_error`.

## P6: a failed `transform_tool_result` withholds the tool result

- **Issue:** HumanTronik/HT-HLM#787 (FU787 Hermes fails open on other hook and middleware errors),
  a follow-up of P5 (HT-HLM#698).
- **Flag:** `ht_patches.p6_tool_result_transform_fail_closed: true` (default off).
- **Why:** `transform_tool_result` is where a plugin can rewrite a tool result before the model
  reads it, and the HLM Harness plugin redacts the job capability there (Hermes's own redaction
  does not cover tool results, HT-HLM spike S1). Upstream skips that step silently when it fails,
  so a server that echoed the capability would put it in the model's context.
- **What it does:** with P6 on, the tool result is replaced by
  `{"error": "BLOCKED: the transform_tool_result hook failed for <tool> (<reason>); the result was withheld"}`
  when any `transform_tool_result` callback
  - raises (`<reason>` is the error's type, never its text),
  - times out under `plugins.hook_callback_timeout`, or is skipped because it is still running
    for that call, is in the suppression window after a timeout, or has reached the
    abandoned-worker cap (`timed out or skipped`),
  - or the transform step itself raises (`_apply_transform_tool_result_hook`,
    `apply_transform_tool_result`) while a `transform_tool_result` callback is registered.

  A withheld result wins over any other callback's string return (upstream takes the first
  string). With no callback registered, or with the flag off, the behaviour is upstream's. A
  config that cannot be read on that path counts as P6 on.
- **Where:** `withhold_on_transform_failure`, `withhold_on_transform_error`,
  `pick_transform_result` and `ResultWithheld` in `hermes_cli/ht_patches.py`; calls in
  `PluginManager._run_hook_callbacks` and `ainvoke_hook` (`hermes_cli/plugins_dispatch.py`),
  `_apply_transform_tool_result_hook` (`model_tools.py`) and `apply_transform_tool_result`
  (`agent/inline_tool_executors.py`). Every tool path reaches one of the last two: registry tools
  inside `handle_function_call`, the agent loops and `AIAgent._invoke_tool` for inline tools.
- **Not covered:** the terminal `post_tool_call` fires before the transform, so it does not carry
  the withholding; the warning log does. A tool whose execution timed out gets Hermes's own timeout
  result, which no transform sees, flag on or off.
- **Limits:** the flag is read from the active profile's config on the failure path; in a
  multiplexed gateway set it in the managed scope (as the HLM image does), not per profile.
- **Removal criterion:** upstream fails `transform_tool_result` closed itself, or the HLM Harness
  plugin stops redacting tool results there. Then drop the flag, this entry and the P6 helpers.

## Paths that stay fail-open (reviewed for HT-HLM#787)

P5 and P6 close the two hook paths that protect data in the HLM configuration (the plugin's
guard and its tool-result redaction). These stay fail-open on purpose:

- **`tool_request` middleware** (`model_tools.py` `_apply_request_middleware`,
  `agent/agent_runtime_helpers.py` `invoke_tool`): an error keeps the unmodified arguments. The
  HLM image registers no middleware (the `hlm_harness` plugin registers none and is the only
  enabled plugin), and the `pre_tool_call` guard still runs on the arguments afterwards.
- **`tool_execution` middleware and the NeMo Relay path**: nothing registers them in the HLM image
  and Relay is not configured.
- **`post_tool_call`** (`model_tools.py` `_emit_post_tool_call_hook` and the agent's terminal
  emit): observers only; the `hlm_harness` plugin registers none. A failure costs an observation
  (and leaves P1's per-run `pre_modify_args` entry, which ends with the run), not a protection.
- **Redaction-pattern registration** (`PluginContext.register_redaction_patterns`): a failure
  warns and registers no pattern, once at startup rather than per call. Log redaction then
  misses the plugin's patterns; the HLM image build's leak scan (HT-HLM `tools/hermes_p1_probe.py`:
  logs, SSE stream and every file of the Hermes home) fails the build if a capability reaches one.
