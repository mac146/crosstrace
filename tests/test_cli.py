"""The crosstrace command (build plan step 5): config validation, record, test, report, exit codes, and the report contract."""
import json
import os
import sys

import pytest

pytest.importorskip("openai")

from crosstrace import cli, config as C, runner      # noqa: E402
from crosstrace.config import FaultSpec              # noqa: E402
from crosstrace.proxy.recording import Exchange, Recording  # noqa: E402
from mockservices.sse_server import serve_in_thread  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "examples", "broken_agents", "agent.py")


@pytest.fixture(scope="module")
def upstream():
    srv = serve_in_thread(chunks=8, gap_ms=0, tail_ms=0, echo=True)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def write_config(d, mode, upstream, extra=""):
    (d / "crosstrace.toml").write_text(f'''
[agent]
command = [{json.dumps(sys.executable)}, {json.dumps(AGENT)}, "--mode", "{mode}", "--question", "{{input}}"]
timeout_s = 30
inputs = ["Where is order 42?"]
env = {{ OPENAI_API_KEY = "sk-test" }}
[record]
upstreams = {{ openai = "{upstream}" }}
[test]
trial_timeout_s = 8
[modes.pr]
faults = ["stream_cut_clean", "server_error", "rate_limit", "context_length"]
jobs = 4
{extra}
''', encoding="utf-8")


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ---- config ---------------------------------------------------------------------------------------------------------------

def test_init_writes_a_valid_config_and_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run(capsys, "init", "--command", "python my_agent.py {input}")[0] == 0
    cfg = C.load()
    assert cfg.command == ["python", "my_agent.py", "{input}"] and set(cfg.modes) >= {"pr", "nightly"}
    assert (tmp_path / ".crosstrace" / ".gitignore").read_text().strip().endswith("reports/")
    code, _, e = run(capsys, "init")
    assert code == 2 and "already exists" in e


@pytest.mark.parametrize("text,needle", [
    ('[agent]\ncommand = ["a"]\ntimeout = 3\n', "unknown key(s) timeout"),
    ('[agent]\ncommand = ["a"]\n[modes.pr]\nfaults = ["stream_cutt"]\n', "unknown fault 'stream_cutt'"),
    ('[agent]\ncommand = ["a"]\n[test]\nallow = ["OK"]\n', "unknown verdict"),
    ('[agent]\ncommand = ["a"]\n[faults.stream_stall]\nstall = 3\n', "unknown parameters"),
    ('[agent]\ncommand = ["a"]\ninputs = ["x", "y"]\n', "no {input} placeholder"),
    ('inputs = ["x"]\n[agent]\ncommand = ["a"]\n', "unknown key(s) inputs"),
    ('[agent]\n', "command is required"),
    ('[agent\n', "crosstrace.toml"),
])
def test_config_errors_are_specific_and_exit_2(tmp_path, monkeypatch, capsys, text, needle):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "crosstrace.toml").write_text(text)
    code, _, e = run(capsys, "test")
    assert code == 2 and needle in e, e


def test_fault_specs_and_persistent_syntax():
    cfg = C.parse({"agent": {"command": ["a"]}, "modes": {"x": {"faults": ["server_error!persistent", {"name": "overloaded", "heal": 2},
                                                                         {"name": "stream_stall", "params": {"stall_s": 5}}]}}}, ".")
    fs = cfg.modes["x"].faults
    assert [(f.name, f.heal, f.label) for f in fs] == [("server_error", None, "server_error!persistent"), ("overloaded", 2, "overloaded"),
                                                       ("stream_stall", 1, "stream_stall")]
    assert fs[2].params == {"stall_s": 5}


# ---- planning -------------------------------------------------------------------------------------------------------------

def _rec(n):
    return Recording(exchanges=[Exchange(idx=i, path="/v1/messages", provider="anthropic", key=str(i), state_key=str(i), request={},
                                         status=200, headers=[], stream=True, body=None, events=["data: {}"]) for i in range(n)])


def test_pr_sampling_is_deterministic_and_keeps_first_and_last():
    recs = [_rec(10)]
    fs = [FaultSpec("stream_cut_clean")]
    a = runner.plan_trials(recs, fs, max_exchanges=4, seed=0)
    assert a == runner.plan_trials(recs, fs, max_exchanges=4, seed=0)
    ks = [k for _, _, k in a]
    assert len(ks) == 4 and ks[0] == 0 and ks[-1] == 9
    assert len(runner.plan_trials(recs, fs, max_exchanges=0)) == 10
    assert [k for _, _, k in runner.plan_trials(recs, fs, only_at=3)] == [3]


def test_trial_budget_comes_from_the_replay_not_the_recorded_wall_time():
    # recorded run: 46 s (slow provider, one failed attempt); replay of the same agent: 3 s
    assert cli.trial_budget([{"replay_seconds": 3.0}, {"replay_seconds": 2.0}]) == 60.0     # floor: a 30 s timeout + 1 retry is no HANG
    assert cli.trial_budget([{"replay_seconds": 25.0}]) == 100.0
    assert cli.trial_budget([{"replay_seconds": None}]) == 60.0


def test_stall_always_outlasts_the_trial_budget():
    assert runner.effective_params("stream_stall", {}, 40.0)["stall_s"] > 40.0
    assert runner.effective_params("idle_kill", {}, 40.0)["idle_s"] < 40.0
    assert runner.effective_params("stream_stall", {"stall_s": 5.0}, 40.0)["stall_s"] == 5.0     # explicit settings win


# ---- end to end -----------------------------------------------------------------------------------------------------------

def test_record_test_report_end_to_end(tmp_path, monkeypatch, capsys, upstream):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path, "naive", upstream)
    code, out, e = run(capsys, "record")
    assert code == 0, out + e
    assert "replay stable" in out and len(list((tmp_path / ".crosstrace" / "recordings").glob("*.json"))) == 1

    md = tmp_path / "summary.md"
    code, out, e = run(capsys, "test", "--md", str(md), "--json", str(tmp_path / "r.json"))
    assert code == 1, out + e                                             # the naive agent hides cut streams
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["schema"] == "crosstrace.report/1" and rep["status"] == "problems"
    assert set(rep["faults"]["stream_cut_clean"]["counts"]) <= {"SILENT", "PARTIAL"}
    assert rep["faults"]["server_error"]["worst"] == "RECOVERED"
    assert any("SILENT" in k or "PARTIAL" in k for k in rep["fixes"])
    lines = [x for v in rep["fixes"].values() for x in v]
    assert len(lines) == len(set(lines))                                  # no advice line printed twice
    assert rep["not_applicable"] == {}                                    # every configured fault applied to this agent
    failing = [t for t in rep["trials"] if t["verdict"] in ("SILENT", "PARTIAL")]
    assert failing and failing[0]["rerun"].startswith("crosstrace test --path 0 --fault stream_cut_clean --at ")
    assert "finish_reason" in out                                         # SDK-specific fix (openai-python, from the User-Agent)
    assert md.read_text(encoding="utf-8").startswith("### CrossTrace: ❌")
    assert (tmp_path / ".crosstrace" / "reports" / "latest.json").exists()

    code, out, _ = run(capsys, "report")
    assert code == 0 and "stream_cut_clean" in out

    code, out, _ = run(capsys, "test", "--fault", "stream_cut_clean", "--at", "2", "--no-save", "--json", str(tmp_path / "one.json"))
    one = json.loads((tmp_path / "one.json").read_text())
    assert code == 1 and len(one["trials"]) == 1 and one["trials"][0]["verdict"] == "PARTIAL"

    # the same recording, the agent fixed: replay uses the command from the current config
    write_config(tmp_path, "good", upstream)
    code, out, e = run(capsys, "test", "--no-save")
    assert code == 0, out + e
    assert "No problems found" in out


def test_unstable_recording_is_a_setup_error_with_the_differing_field(tmp_path, monkeypatch, capsys, upstream):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path, "nondeterministic", upstream)
    code, out, _ = run(capsys, "record")
    assert code == 3 and "NOT stable" in out and "content" in out and "masks" in out
    code, out, _ = run(capsys, "test", "--no-save")
    assert code == 3 and "replay is not stable" in out


def test_masks_make_the_unstable_agent_stable(tmp_path, monkeypatch, capsys, upstream):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path, "nondeterministic", upstream, extra='[replay]\nmasks = ["\\\\(ref [0-9.]+\\\\)"]\n')
    code, out, e = run(capsys, "record")
    assert code == 0, out + e


def test_no_recordings_is_a_setup_error(tmp_path, monkeypatch, capsys, upstream):
    monkeypatch.chdir(tmp_path)
    write_config(tmp_path, "good", upstream)
    code, _, e = run(capsys, "test")
    assert code == 3 and "crosstrace record" in e


def test_proxy_url_placeholder_for_agents_with_their_own_base_url_variable():
    spec = runner.AgentSpec(command=["x"], env={"OPENCODE_BASE_URL": "{proxy_url}/v1", "OTHER": "plain"})
    env = spec.environ("http://127.0.0.1:5555", replay=True)
    assert env["OPENCODE_BASE_URL"] == "http://127.0.0.1:5555/v1" and env["OTHER"] == "plain"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:5555/v1" and env["OPENAI_API_KEY"] == "sk-crosstrace-replay"


def test_terminal_without_unicode_gets_ascii_marks():
    import io
    from crosstrace.report import for_terminal
    cp1252 = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    assert for_terminal("✗ stream_cut_clean · ✓ ok", cp1252) == "x stream_cut_clean | + ok"
    assert for_terminal("✗ a", io.TextIOWrapper(io.BytesIO(), encoding="utf-8")) == "✗ a"


def test_a_crash_is_exit_4_not_exit_1(monkeypatch, capsys):
    def boom(a):
        raise RuntimeError("bug")
    monkeypatch.setattr(cli, "cmd_faults", boom)
    code, _, e = run(capsys, "faults")
    assert code == 4 and "internal error" in e


def test_faults_lists_the_catalog(capsys):
    code, out, _ = run(capsys, "faults")
    assert code == 0 and "stream_cut_clean" in out and "!persistent" in out


def test_the_openai_agents_sdk_is_recognised_and_gets_its_own_timeout_advice():
    """Found on a real agent: the SDK identifies itself as "Agents/Python <version>", and the generic advice does not say how to set its client."""
    from crosstrace import fixes
    reqs = [{"user-agent": "Agents/Python 0.24.0"}]
    assert fixes.sdk_family(reqs) == "openai-agents"
    assert any("set_default_openai_client" in line for line in fixes.advice("HANG", "stream_stall", reqs))
    assert fixes.sdk_family([{"user-agent": "OpenAI/Python 3.50.0"}]) == "openai-python"              # unchanged


def test_pydantic_ai_is_not_mistaken_for_the_javascript_vercel_sdk():
    """Found on a real agent: "pydantic-ai/2.68.0" contains "ai/", so its HANG advice was `AbortSignal.timeout(...)`, a JavaScript API."""
    from crosstrace import fixes
    assert fixes.sdk_family([{"user-agent": "pydantic-ai/2.68.0"}]) == "pydantic-ai"
    assert any("model_settings" in line for line in fixes.advice("HANG", "stream_stall", [{"user-agent": "pydantic-ai/2.68.0"}]))
    assert fixes.sdk_family([{"user-agent": "ai/5.0.1"}]) == "vercel-ai" and fixes.sdk_family([{"user-agent": "ai-sdk/openai/2.0"}]) == "vercel-ai"


# ---- which SDK the advice is for: recognised from a table, overridable, and never a wrong guess ---------------------------------------

REAL_USER_AGENTS = {                  # seen on real traffic in this project's own runs
    "OpenAI/Python 3.50.0": "openai-python", "OpenAI/Python 2.54.0": "openai-python",       # also what CrewAI and LangChain send
    "Agents/Python 0.24.0": "openai-agents", "pydantic-ai/2.68.0": "pydantic-ai",
    "Anthropic/Python 2.8.0": "anthropic-python", "OpenAI/JS 7.55.0": "openai-js", "Anthropic/JS 1.8.0": "anthropic-js",
    "ai/7.0.169": "vercel-ai", "ai-sdk/openai/4.0.114 ai/7.0.169": "vercel-ai",
}


def test_every_user_agent_seen_on_real_traffic_maps_to_its_sdk():
    from crosstrace import fixes
    for ua, family in REAL_USER_AGENTS.items():
        assert fixes.detect_sdk([{"user-agent": ua}])["family"] == family, ua


def test_a_user_agent_that_only_contains_the_letters_ai_is_not_the_vercel_sdk():
    from crosstrace import fixes
    for ua in ("rails/6.1", "langchain-ai/0.3", "my-ai/1.0", "mail/2.0", "python-httpx/0.28.1", ""):
        d = fixes.detect_sdk([{"user-agent": ua}])
        assert d["family"] is None, ua


def test_an_unrecognised_sdk_gets_every_option_labelled_not_one_guess():
    from crosstrace import fixes
    lines = fixes.advice("HANG", "stream_stall", [{"user-agent": "python-httpx/0.28.1"}])
    first = lines[0]
    assert "could not be told" in first and all(k in first for k in ("openai-python:", "pydantic-ai:", "vercel-ai:", "anthropic-js:"))
    assert "AbortSignal" in first and "model_settings" in first               # each fix is there, under its own label
    assert fixes.advice("HANG", "stream_stall", [])[0] == first               # no traffic at all: the same honest answer
    note = fixes.describe_sdk([{"user-agent": "python-httpx/0.28.1"}])
    assert "not recognised" in note and "python-httpx/0.28.1" in note and "[agent] sdk" in note


def test_the_configured_sdk_wins_over_the_user_agent():
    from crosstrace import fixes
    reqs = [{"user-agent": "OpenAI/Python 3.50.0"}]                          # CrewAI's user agent is the plain OpenAI one
    assert fixes.detect_sdk(reqs, "pydantic-ai") == {"family": "pydantic-ai", "user_agent": "OpenAI/Python 3.50.0", "source": "config"}
    assert "model_settings" in fixes.advice("HANG", "stream_stall", reqs, "pydantic-ai")[0]
    assert "set in [agent] sdk" in fixes.describe_sdk(reqs, "pydantic-ai")
    assert "from the user-agent" in fixes.describe_sdk(reqs)


def test_config_accepts_a_known_sdk_and_rejects_a_misspelt_one():
    from crosstrace import config as C
    base = {"agent": {"command": ["python", "a.py"]}}
    assert C.parse({"agent": {**base["agent"], "sdk": "openai-agents"}}, ".").sdk == "openai-agents"
    assert C.parse(base, ".").sdk is None
    with pytest.raises(C.ConfigError, match="openai-agent"):
        C.parse({"agent": {**base["agent"], "sdk": "openai-agent"}}, ".")


def test_the_report_says_which_sdk_it_wrote_the_advice_for():
    from crosstrace import report as R
    from crosstrace.verdict import TrialResult
    from crosstrace import config as C
    cfg = C.parse({"agent": {"command": ["python", "a.py"], "inputs": ["x"]}}, ".")
    mode = cfg.modes["pr"]
    from crosstrace.proxy.recording import Recording
    rec = Recording(exchanges=[])
    r = TrialResult(path=0, fault="stream_stall", at=0, verdict="HANG", requests=[{"provider": "openai", "user-agent": "pydantic-ai/2.68.0"}])
    rep = R.build(cfg, mode, [rec], [{"stable": True, "problems": []}], [r], 1.0, 0, {})
    assert rep["sdk_note"].startswith("SDK: pydantic-ai (from the user-agent 'pydantic-ai/2.68.0'")
    assert "SDK: pydantic-ai" in R.for_terminal(R.render_text(rep)) and "SDK: pydantic-ai" in R.render_markdown(rep)
