"""Fault accounting (build plan section 6): what did the agent do after the fault?

The verdict comes from the agent's next observable move, not from comparing final answers:
  same exchange again (retry or fallback)        -> recovery path: RECOVERED, or a bad retry (RETRY_STORM, IGNORED_RETRY_AFTER, WRONG_RETRY)
  a different model request, exchange not done   -> SILENT (it continued with missing or partial data)
  exit != 0, exchange not done                   -> SURFACED
  exit 0, exchange not done                      -> PARTIAL (it printed the cut text) or ENDED_EARLY (output shown for review)
  nothing within the budget                      -> HANG
Inputs are the proxy's request log, the fault plan's attempt counts and the agent's exit code and output.
"""
import email.utils
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

from crosstrace.proxy.faults import NON_RETRYABLE, json_body, text_slot, parse_event

OK = {"RECOVERED", "SURFACED"}
REVIEW = {"RECOVERED_DIFFERENT", "ENDED_EARLY", "REPROMPTED"}
PROBLEM = {"SILENT", "PARTIAL", "HANG", "RETRY_STORM", "IGNORED_RETRY_AFTER", "WRONG_RETRY", "DUPLICATE"}
SETUP = {"REPLAY_UNSTABLE", "NOT_REACHED", "N/A"}
ORDER = ["DUPLICATE", "HANG", "WRONG_RETRY", "RETRY_STORM", "IGNORED_RETRY_AFTER", "SILENT", "PARTIAL", "ENDED_EARLY", "REPROMPTED", "RECOVERED_DIFFERENT",
         "SURFACED", "RECOVERED", "NOT_REACHED", "REPLAY_UNSTABLE", "N/A"]
STORM_LIMIT = 6            # attempts at one exchange above which retries are a storm
LONG_RETRY_AFTER_S = 120   # longer server-directed waits are not judged by spacing (HANG covers them)


@dataclass
class TrialResult:
    path: int                  # which recording
    fault: str
    at: int                    # exchange index
    verdict: str
    detail: str = ""
    attempts: int = 0
    retry_gaps_s: list = field(default_factory=list)
    exit_code: int = None
    seconds: float = 0.0
    stdout: str = ""
    requests: list = field(default_factory=list)

    @property
    def trial_id(self):
        return f"--path {self.path} --fault {self.fault} --at {self.at}"

    def to_dict(self):
        return asdict(self)


def body_answer_text(ex):
    """The answer text of a non-streamed exchange ('' when it has none)."""
    obj = json_body(ex)
    slot = text_slot(obj) if obj else None
    return slot[0][slot[1]] if slot else ""


def delivered_text(ex, n):
    """Text content of the first n events of a streamed exchange (OpenAI chat, Anthropic, Responses)."""
    out = []
    for text in ex.events[:n]:
        _, _, obj = parse_event(text)
        if not isinstance(obj, dict):
            continue
        for c in obj.get("choices") or []:
            out.append(((c or {}).get("delta") or {}).get("content") or "")
        d = obj.get("delta")
        if isinstance(d, dict) and d.get("type") == "text_delta":
            out.append(d.get("text", ""))
        if obj.get("type") == "response.output_text.delta":
            out.append(obj.get("delta", ""))
    return "".join(out)


def _retry_after_s(entry):
    ra = (entry.get("fault_detail") or {}).get("retry_after")
    if ra is None:
        return None
    try:
        return float(ra)
    except ValueError:
        t = email.utils.parsedate_to_datetime(ra)
        return max(0.0, t.timestamp() - entry["t"])


def _tool_calls(body):
    """[(tool name, arguments as JSON text)] of every tool call in a request's conversation (OpenAI chat, Anthropic, Responses)."""
    out = []
    for m in (body or {}).get("messages") or []:
        for tc in (m or {}).get("tool_calls") or []:
            f = tc.get("function") or {}
            out.append((f.get("name"), f.get("arguments")))
        for b in (m or {}).get("content") if isinstance((m or {}).get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                out.append((b.get("name"), json.dumps(b.get("input"), separators=(",", ":"))))
    for it in (body or {}).get("input") if isinstance((body or {}).get("input"), list) else []:
        if isinstance(it, dict) and it.get("type") == "function_call":
            out.append((it.get("name"), it.get("arguments")))
    return out


def _new_tool_calls(faulted_request, next_request):
    """Tool calls in the agent's next request that were not in the faulted request: the ones it took from the faulted response."""
    before = _tool_calls(faulted_request)
    return _tool_calls(next_request)[len(before):] if next_request else []


def _conversation(body):
    """The message list of a model request (chat completions and Anthropic: messages; Responses: input), or None."""
    if isinstance(body, dict):
        for key in ("messages", "input"):
            if isinstance(body.get(key), list):
                return body[key]
    return None


def _runs_something(message):
    """True when a message carries a tool call or a tool result: the agent did something with the failed reply, it did not just talk about it."""
    if not isinstance(message, dict):
        return True
    if message.get("role") == "tool" or message.get("tool_calls") or message.get("type") in ("function_call", "function_call_output"):
        return True
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for b in content)


def _message_text(message):
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "").strip()


# What a framework's own argument validation says about a tool call whose JSON was cut off (pydantic: json_invalid, "Invalid JSON: EOF
# while parsing"; Python: "Expecting value", "Unterminated string"; Node: "Unexpected end of JSON input"; and the common wordings).
_REJECTION = re.compile(r"json_invalid|invalid json|invalid_json|jsondecodeerror|unexpected end of json|unterminated string|eof while parsing|"
                        r"expecting (value|property name|',' delimiter|':' delimiter)|failed to parse|could not parse|not valid json|malformed",
                        re.IGNORECASE)


def _tool_result_texts(body):
    """The text of every tool result in a request's conversation, in order (chat completions, Anthropic and Responses)."""
    out = []
    for m in _conversation(body) or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            out.append(_message_text(m))
        elif m.get("type") == "function_call_output":
            out.append(str(m.get("output") or ""))
        elif isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    out.append(" ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c or ""))
    return out


def _rejected_tool_results(faulted_request, next_request):
    """The tool results the agent added after the faulted reply, when every one of them is the framework refusing the cut arguments.

    Such an agent validated the arguments, did not run the tool, and told the model to try again: it handled the failure. If any added
    result looks like a normal result, or there is none, the tool may have run on the cut arguments and this returns [] (SILENT)."""
    added = _tool_result_texts(next_request)[len(_tool_result_texts(faulted_request)):]
    return added if added and all(_REJECTION.search(t) for t in added) else []


def _reprompt(faulted_request, next_request):
    """The text the agent added to tell the model its last reply failed, or None.

    A re-prompt is the faulted request unchanged, then at most the failed reply and ONE new message to the model, and nothing that runs a tool.
    CrossTrace cannot tell a message that reports the failure from one that reports what the agent did with it (a text-protocol agent may
    send its own observations as user messages), so the verdict is a review: the text is shown for a person to judge."""
    before, after = _conversation(faulted_request), _conversation(next_request)
    if before is None or after is None or after[:len(before)] != before:
        return None
    added = after[len(before):]
    if not 1 <= len(added) <= 2 or any(_runs_something(m) for m in added):
        return None
    # the message that tells the model is a user message, or a system/developer one (pydantic-ai adds "Please return text or call a tool.")
    if added[-1].get("role") not in ("user", "system", "developer") or (len(added) == 2 and added[0].get("role") != "assistant"):
        return None
    return _message_text(added[-1])


def classify(fault, at, recording, requests, exit_code, stdout, hang, healthy_stdout, path=0, seconds=0.0):
    ex = recording.exchanges[at]
    model = [r for r in requests if r.get("provider")]
    mine = [r for r in model if r.get("idx") == at]
    res = TrialResult(path=path, fault=fault, at=at, verdict="N/A", exit_code=exit_code, seconds=round(seconds, 2),
                      stdout=stdout, requests=requests)
    before = []
    for r in model:
        if r.get("idx") == at:
            break
        before.append(r)
    if any(r.get("how") == "miss" for r in before):
        res.verdict, res.detail = "REPLAY_UNSTABLE", "a request before the faulted exchange matched no recording"
        return res
    first = next((r for r in mine if "fault" in r), None)
    if first is None:
        if any(r.get("fault_na") for r in mine):
            res.verdict, res.detail = "N/A", f"{fault} does not apply to exchange {at}"
        elif hang:
            res.verdict, res.detail = "HANG", f"exchange {at} was never reached within {seconds:.0f} s"
        else:
            res.verdict, res.detail = "NOT_REACHED", f"the agent never made exchange {at} on this run"
        return res
    i0 = requests.index(first)
    after = [r for r in requests[i0 + 1:] if r.get("provider")]
    attempts = [first] + [r for r in after if r.get("idx") == at]
    res.attempts = len(attempts)
    res.retry_gaps_s = [round(b["t"] - a["t"], 3) for a, b in zip(attempts, attempts[1:])]
    success_pos = next((k for k, r in enumerate(after) if r.get("idx") == at and "fault" not in r), None)
    other_pos = next((k for k, r in enumerate(after) if r.get("idx") != at), None)
    via = " via fallback" if any(r.get("how") == "fallback" for r in attempts) else ""
    tries = f"{res.attempts - 1} retr{'y' if res.attempts == 2 else 'ies'}{via}" if res.attempts > 1 else "no retry"
    # An agent that starts over: before it retries the failed call it re-sends earlier exchanges, each one an exact repeat of a recorded
    # request. Nothing is missing from its context, so this is a recovery (the work done twice is for the write check to judge).
    if success_pos is not None and other_pos is not None and other_pos < success_pos:
        between = [r for r in after[:success_pos] if r.get("idx") != at]
        if all(r.get("how") == "repeat" and r.get("idx") is not None and r["idx"] < at for r in between):
            tries += f", after starting over from exchange {min(r['idx'] for r in between)}"
            other_pos = None

    if hang:
        res.verdict = "HANG"
        res.detail = f"no exit within {seconds:.0f} s ({tries}" + (f", last gap {res.retry_gaps_s[-1]} s" if res.retry_gaps_s else "") + ")"
        return res
    if fault in NON_RETRYABLE and res.attempts > 1:
        res.verdict, res.detail = "WRONG_RETRY", f"retried a non-retryable error ({tries}, first after {res.retry_gaps_s[0]} s)"
        return res
    if res.attempts > STORM_LIMIT:
        gaps = sorted(res.retry_gaps_s)
        res.verdict, res.detail = "RETRY_STORM", f"{res.attempts} attempts at exchange {at}, median gap {gaps[len(gaps) // 2]} s"
        return res
    ra = _retry_after_s(first)
    if ra is not None and ra <= LONG_RETRY_AFTER_S and res.retry_gaps_s and res.retry_gaps_s[0] < 0.9 * ra:
        res.verdict, res.detail = "IGNORED_RETRY_AFTER", f"retried after {res.retry_gaps_s[0]} s; Retry-After was {ra:.0f} s"
        return res
    if other_pos is not None and (success_pos is None or other_pos < success_pos):
        nxt = after[other_pos]
        res.verdict = "SILENT"
        cut = _new_tool_calls(ex.request, nxt.get("request")) if nxt.get("idx") is None else []
        # only when a reply arrived (HTTP 200) and was defective: after an error status there is no reply to tell the model about, so an agent
        # that carries on is inventing context, whatever message it adds
        said = _reprompt(ex.request, nxt.get("request")) if nxt.get("idx") is None and not cut and first.get("status") == 200 else None
        rejected = _rejected_tool_results(ex.request, nxt.get("request")) if cut and first.get("status") == 200 else []
        if rejected:                   # the framework refused the cut arguments and told the model: the tool did not run
            res.verdict = "REPROMPTED"
            shown = rejected[0] if len(rejected[0]) <= 160 else rejected[0][:157] + "..."
            res.detail = (f"the cut tool call was refused, not run, and the model was told: \"{shown}\" instead of retrying exchange {at} "
                          f"({tries}); check that this stops if the model keeps sending bad arguments")
        elif said:                     # it told the model about the failure instead of retrying the same call
            res.verdict = "REPROMPTED"
            shown = said if len(said) <= 160 else said[:157] + "..."
            res.detail = (f"told the model the reply failed instead of retrying exchange {at}: \"{shown}\" ({tries}); "
                          f"check that nothing ran on the bad reply and that this stops if the model keeps failing")
        elif cut:                        # the clearest harm: a tool ran on arguments from a response that never finished
            name, args = cut[0]
            res.detail = f"ran tool {name} with arguments {args} from a response that never finished, instead of retrying exchange {at} ({tries})"
        elif nxt.get("idx") is None:   # its next request is not in the recording: its context changed (partial or missing data)
            res.detail = f"made its next model call with the failed response in its context instead of retrying exchange {at} ({tries})"
        else:
            res.detail = f"moved on to exchange {nxt['idx']} without a successful exchange {at} ({tries})"
        return res
    if success_pos is None:
        if exit_code != 0:
            res.verdict, res.detail = "SURFACED", f"exited {exit_code} ({tries})"
            return res
        detail = first.get("fault_detail") or {}
        n = detail.get("events_sent")
        part = delivered_text(ex, n).strip() if (ex.stream and n) else ""
        full = delivered_text(ex, len(ex.events)).strip() if ex.stream else ""
        if not ex.stream and first.get("fault") == "length_cut" and detail.get("chars_sent"):
            full = body_answer_text(ex).strip()                 # a cut non-streamed answer: the first chars_sent characters were delivered
            part = full[:detail["chars_sent"]].strip()
        if part and part in stdout and (not full or full not in stdout):
            res.verdict, res.detail = "PARTIAL", f"printed the cut answer ({len(part)} of {len(full)} chars) and exited 0"
        elif not stdout.strip() and (healthy_stdout or "").strip():
            res.verdict, res.detail = "ENDED_EARLY", f"printed nothing and exited 0 after exchange {at} failed (a healthy run prints an answer)"
        else:
            res.verdict, res.detail = "ENDED_EARLY", f"exited 0 without a successful exchange {at}; output needs review"
        return res
    if exit_code != 0:
        res.verdict, res.detail = "SURFACED", f"recovered exchange {at} ({tries}) but exited {exit_code}"
    elif stdout.strip() == (healthy_stdout or "").strip():
        res.verdict, res.detail = "RECOVERED", tries + (f", first after {res.retry_gaps_s[0]} s" if res.retry_gaps_s else "")
    else:
        res.verdict, res.detail = "RECOVERED_DIFFERENT", f"recovered ({tries}) but the output differs from the healthy run"
    return res


def early_silent(at, requests):
    """True as soon as the agent has moved past exchange `at` without a successful attempt (the verdict cannot change any more)."""
    seen_fault, success = False, False
    for r in list(requests):
        if not r.get("provider"):
            continue
        if r.get("idx") == at:
            if "fault" in r:
                seen_fault = True
            elif seen_fault:
                success = True
        elif seen_fault and not success:
            return True
    return False


def attempts_at(at, requests):
    return sum(1 for r in list(requests) if r.get("provider") and r.get("idx") == at)


def summarize(results):
    """{fault: {"counts": Counter(verdict), "applicable": n, "problems": [trial ids]}}"""
    out = {}
    for r in results:
        s = out.setdefault(r.fault, {"counts": Counter(), "applicable": 0, "problems": [], "review": []})
        s["counts"][r.verdict] += 1
        if r.verdict not in SETUP:
            s["applicable"] += 1
        if r.verdict in PROBLEM:
            s["problems"].append(r)
        if r.verdict in REVIEW:
            s["review"].append(r)
    return out


def worst(counts):
    return min((v for v in counts if counts[v]), key=ORDER.index, default="N/A")


def to_json(results):
    return json.dumps([r.to_dict() for r in results], default=str, indent=1)


def now():
    return time.time()
