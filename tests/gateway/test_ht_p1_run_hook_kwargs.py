"""HT patch P1 end to end: a real ``POST /v1/runs`` (with and without the session authority)
gives the plugin tool hooks the run id and the caller's ``X-Hermes-Session-Key`` only when
``ht_patches.p1_run_hook_kwargs`` is on; ``post_tool_call`` also gets the pre-``modify`` args."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli.ht_patches import P1_HOOK_KWARGS as P1_KWARGS, RUN_METADATA, TOOL_HOOKS

KEY = "job-1.a1:htc_0123456789abcdefghijkl"


def _run(tmp_path, *, authority, flag):
    repo = Path(__file__).resolve().parents[2]
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    state.mkdir()
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(repo),
               P1_KEY=KEY, P1_AUTHORITY="1" if authority else "0", P1_FLAG=flag or "")
    result = subprocess.run([sys.executable, str(Path(__file__).parent / "fixtures" / "ht_p1_run_hooks_peer.py")],
                            cwd=repo, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=120, check=False)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    receipt = json.loads((state / "receipt.json").read_text())
    assert receipt["status"]["status"] == "completed", receipt
    assert receipt["authority"] is authority
    calls = [json.loads(line) for line in (state / "hook_calls.jsonl").read_text().splitlines()]
    return receipt, {c["hook"]: c for c in calls if c.get("tool_name") == "read_file"}


@pytest.mark.parametrize("authority", [True, False], ids=["session-authority", "direct"])
def test_p1_on_tool_hooks_get_the_run_identity(tmp_path, authority):
    receipt, calls = _run(tmp_path, authority=authority, flag="on")
    assert set(calls) == TOOL_HOOKS
    for name, kw in calls.items():
        assert kw["run_id"] == receipt["run_id"], name
        assert kw["gateway_session_key"] == KEY, name
        assert kw["run_metadata"] == RUN_METADATA, name
        assert kw["session_id"] == "job-1.a1", name
    post = calls["post_tool_call"]
    assert post["args"]["path"] != "MODEL_PATH.txt"  # the hook's modify reached the tool
    assert post["pre_modify_args"] == {"path": "MODEL_PATH.txt"}


@pytest.mark.parametrize("flag", ["", "off"], ids=["unset", "false"])
@pytest.mark.parametrize("authority", [True, False], ids=["session-authority", "direct"])
def test_p1_off_tool_hooks_are_upstream_shaped(tmp_path, authority, flag):
    _, calls = _run(tmp_path, authority=authority, flag=flag)
    assert set(calls) == TOOL_HOOKS
    for name, kw in calls.items():
        assert not P1_KWARGS & set(kw), (name, sorted(kw))
        assert KEY not in json.dumps(kw), name
