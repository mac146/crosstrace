"""crosstrace.toml: what to run, which inputs, which faults per mode. Every key is validated; unknown keys are errors (typos fail loudly).

TOML because Python 3.11+ reads it natively (no dependency) and it is typed.
"""
import os
import shlex
import tomllib
from dataclasses import dataclass, field

from crosstrace.proxy.faults import CATALOG, make_fault

CONFIG_NAME = "crosstrace.toml"
STATE_DIR = ".crosstrace"

# PR mode: the faults that most often reveal hidden failures, fast and deterministic. Nightly: the whole catalog.
PR_FAULTS = ["stream_cut_clean", "tool_args_cut", "stream_stall", "length_cut", "malformed_body", "server_error",
             {"name": "overloaded", "heal": "never"}, "rate_limit", "reset_before_response", "context_length", "lost_response"]
NIGHTLY_EXTRA = ["stream_cut_fin", "stream_cut_rst", "stream_error_event", "malformed_event", "idle_kill", "slow_first_byte", "unavailable",
                 "rate_limit_http_date", "auth", "empty_content", {"name": "server_error", "heal": "never"}]
DEFAULT_MODES = {
    "pr": {"faults": PR_FAULTS, "max_exchanges": 4, "stability_runs": 1, "jobs": 4},
    "nightly": {"faults": PR_FAULTS + NIGHTLY_EXTRA, "max_exchanges": 0, "stability_runs": 2, "jobs": 4},
}
VERDICTS_ALLOWED_IN_ALLOW = {"ENDED_EARLY", "RECOVERED_DIFFERENT", "SILENT", "PARTIAL", "HANG", "RETRY_STORM", "IGNORED_RETRY_AFTER",
                             "WRONG_RETRY", "DUPLICATE", "REPROMPTED", "DIVERGED"}

TEMPLATE = '''# CrossTrace configuration. Docs: crosstrace --help, crosstrace faults
# Recordings in .crosstrace/recordings/ are meant to be committed (CI replays them without API keys).
# They contain the prompts and model answers of your healthy runs: add masks below, review before committing.

[agent]
# The command that runs your agent once. "{input}" is replaced by each input below.
command = {command}
# Each input is recorded as one path through your agent. More inputs = more paths covered.
inputs = ["Where is order 42 and when will it arrive?"]
timeout_s = 120            # a healthy run must finish within this
# sdk = "openai-python"   # the SDK the fix advice is written for; default: read from the User-Agent (wrong or unrecognised? set it here)
# cwd = "."
# env = { MY_SETTING = "1" }    # "{proxy_url}" is replaced by the proxy, e.g. MY_BASE_URL = "{proxy_url}/v1"

[record]
runs = 1                   # healthy recordings per input (different sampled paths)
# upstreams = { openai = "https://api.openai.com", anthropic = "https://api.anthropic.com" }

[replay]
# Regexes for values that change between runs (timestamps and UUIDs are masked already).
masks = []

[test]
# Verdicts that should not fail the build (e.g. "ENDED_EARLY" if your agent ends politely on errors).
allow = []
# continue = false         # default true: after a fault, a request that matches nothing recorded is answered with the next healthy reply, so the
#                          # run reaches its real end and the verdict comes from how it ended (not from the shape of a message)
# trial_timeout_s = 90     # a trial running longer is a HANG. Default: 4x the slowest fault-free replay, at least 60 s

# [modes.pr]               # override the built-in modes: faults, max_exchanges (0 = all), stability_runs, jobs
# faults = ["stream_cut_clean", "server_error", { name = "overloaded", heal = "never" }]

# [faults.stream_stall]    # fault parameters, see: crosstrace faults
# stall_s = 90

# MCP tools: launch your MCP server as ["crosstrace", "mcp-wrap", "--", <its usual command>] (see docs/cli.md).
# CrossTrace guesses which tools write (send, submit, pay, delete, click, ...); list the ones it gets wrong.
# [mcp]
# writes = ["run_report"]
# reads = ["refund_status"]
'''


class ConfigError(Exception):
    pass


@dataclass
class FaultSpec:
    name: str
    heal: object = 1                  # attempts before it heals; None = never
    params: dict = field(default_factory=dict)

    @property
    def label(self):
        return self.name + ("!persistent" if self.heal is None else "")


@dataclass
class Mode:
    name: str
    faults: list
    max_exchanges: int
    stability_runs: int
    jobs: int


@dataclass
class Config:
    root: str
    command: list
    inputs: list
    timeout_s: float
    cwd: str
    env: dict
    runs: int
    upstreams: dict
    masks: list
    allow: set
    trial_timeout_s: float
    modes: dict
    fault_params: dict
    mcp: dict = field(default_factory=dict)          # {"writes": [...], "reads": [...]}: tool names that override the write heuristic
    sdk: str = None                                  # the SDK family the fix advice is written for; None: read from the User-Agent
    continuation: bool = True                        # answer requests that match nothing after a fault, so the run reaches its real end

    @property
    def state_dir(self):
        return os.path.join(self.root, STATE_DIR)

    @property
    def recordings_dir(self):
        return os.path.join(self.state_dir, "recordings")

    @property
    def reports_dir(self):
        return os.path.join(self.state_dir, "reports")

    def command_for(self, inp):
        return [part.replace("{input}", inp) for part in self.command]


def _check_keys(where, d, allowed):
    unknown = set(d) - set(allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}")


def _http_servers(value):
    """[mcp.http]: {name: url} for remote MCP servers (streamable HTTP)."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.startswith(("http://", "https://"))
                                              for k, v in value.items()):
        raise ConfigError("[mcp.http]: name = \"https://host/path\" for each remote MCP server")
    return dict(value)


def _sdk(value):
    from crosstrace.fixes import TIMEOUT, COMPLETION
    if value is None:
        return None
    known = sorted(set(TIMEOUT) | set(COMPLETION))
    if value not in known:
        raise ConfigError(f"[agent] sdk: {value!r} is not one of {', '.join(known)}")
    return value


def _fault_spec(item, where):
    if isinstance(item, str):
        name, heal = (item[:-len("!persistent")], None) if item.endswith("!persistent") else (item, 1)
        params = {}
    elif isinstance(item, dict):
        _check_keys(where, item, {"name", "heal", "params"})
        name = item.get("name")
        h = item.get("heal", 1)
        heal = None if h in ("never", None) else h
        if heal is not None and (not isinstance(heal, int) or heal < 1):
            raise ConfigError(f"{where}: heal must be a positive number of failed attempts or \"never\"")
        params = item.get("params", {})
    else:
        raise ConfigError(f"{where}: a fault is a name or {{ name = ..., heal = ..., params = {{...}} }}")
    if name not in CATALOG:
        raise ConfigError(f"{where}: unknown fault {name!r}; run `crosstrace faults` for the list")
    return FaultSpec(name, heal, dict(params))


def load(path=None):
    path = path or os.path.join(os.getcwd(), CONFIG_NAME)
    if not os.path.exists(path):
        raise ConfigError(f"{path} not found; run `crosstrace init` first")
    try:
        with open(path, "rb") as f:
            d = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}")
    return parse(d, os.path.dirname(os.path.abspath(path)))


def parse(d, root):
    _check_keys("crosstrace.toml", d, {"agent", "record", "replay", "test", "modes", "faults", "mcp"})
    mcp = d.get("mcp") or {}
    _check_keys("[mcp]", mcp, {"writes", "reads", "http"})
    for key in ("writes", "reads"):
        if not isinstance(mcp.get(key, []), list) or not all(isinstance(x, str) for x in mcp.get(key, [])):
            raise ConfigError(f"[mcp] {key}: a list of tool names")
    agent = d.get("agent") or {}
    _check_keys("[agent]", agent, {"command", "inputs", "timeout_s", "cwd", "env", "sdk"})
    cmd = agent.get("command")
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if not cmd or not all(isinstance(c, str) for c in cmd):
        raise ConfigError("[agent] command is required: a list like [\"python\", \"agent.py\", \"{input}\"] or a string")
    inputs = agent.get("inputs") or [""]
    if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
        raise ConfigError("[agent] inputs: a list of strings")
    if len(inputs) > 1 and not any("{input}" in c for c in cmd):
        raise ConfigError("[agent] has several inputs, but its command has no {input} placeholder")
    rec = d.get("record") or {}
    _check_keys("[record]", rec, {"runs", "upstreams"})
    rep = d.get("replay") or {}
    _check_keys("[replay]", rep, {"masks"})
    test = d.get("test") or {}
    _check_keys("[test]", test, {"allow", "trial_timeout_s", "continue"})
    allow = set(test.get("allow", []))
    bad = allow - VERDICTS_ALLOWED_IN_ALLOW
    if bad:
        raise ConfigError(f"[test] allow: unknown verdict(s) {sorted(bad)}; one of {sorted(VERDICTS_ALLOWED_IN_ALLOW)}")
    upstreams = rec.get("upstreams", {})
    _check_keys("[record] upstreams", upstreams, {"openai", "anthropic"})

    fault_params = d.get("faults") or {}
    for name, params in fault_params.items():
        if name not in CATALOG:
            raise ConfigError(f"[faults.{name}]: unknown fault; run `crosstrace faults` for the list")
        try:
            make_fault(name, **params)
        except ValueError as e:
            raise ConfigError(f"[faults.{name}]: {e}")

    modes = {}
    user_modes = d.get("modes") or {}
    for mname in set(DEFAULT_MODES) | set(user_modes):
        m = {**DEFAULT_MODES.get(mname, DEFAULT_MODES["nightly"]), **user_modes.get(mname, {})}
        _check_keys(f"[modes.{mname}]", user_modes.get(mname, {}), {"faults", "max_exchanges", "stability_runs", "jobs"})
        faults = [_fault_spec(x, f"[modes.{mname}] faults[{i}]") for i, x in enumerate(m["faults"])]
        for fs in faults:
            merged = {**fault_params.get(fs.name, {}), **fs.params}
            try:
                make_fault(fs.name, **merged)
            except ValueError as e:
                raise ConfigError(f"[modes.{mname}] {fs.name}: {e}")
            fs.params = merged
        modes[mname] = Mode(mname, faults, int(m["max_exchanges"]), max(1, int(m["stability_runs"])), max(1, int(m["jobs"])))

    return Config(root=root, command=list(cmd), inputs=list(inputs), timeout_s=float(agent.get("timeout_s", 120)),
                  cwd=os.path.join(root, agent.get("cwd", ".")), env={k: str(v) for k, v in (agent.get("env") or {}).items()},
                  runs=max(1, int(rec.get("runs", 1))), upstreams=dict(upstreams), masks=list(rep.get("masks", [])), allow=allow,
                  trial_timeout_s=test.get("trial_timeout_s"), modes=modes, fault_params=fault_params,
                  mcp={"writes": list(mcp.get("writes", [])), "reads": list(mcp.get("reads", [])), "http": _http_servers(mcp.get("http"))}, continuation=bool(test.get("continue", True)), sdk=_sdk(agent.get("sdk")))


def template(command):
    return TEMPLATE.replace("{command}", "[" + ", ".join(f'"{c}"' for c in command) + "]")
