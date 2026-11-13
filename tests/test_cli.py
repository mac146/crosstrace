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
