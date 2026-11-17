import re

from crosstrace.masks import suggest_mask
from crosstrace.proxy.providers import compile_masks, normalize
from crosstrace.runner import json_diff


def works(old, new):
    rx = suggest_mask(old, new)
    assert rx, (old, new)
    c = re.compile(rx)
    assert c.sub("<masked>", old) == c.sub("<masked>", new) and c.search(old)
    return rx


def test_a_request_id_in_a_prompt_gets_a_mask_that_equalises_it():
    works("Trace req_8f3a91 for order 42", "Trace req_c07d55 for order 42")
    works("session 1731500001 started", "session 1731500942 started")
    works("run-3-of-7", "run-4-of-7")


def test_the_mask_keeps_the_stable_parts_visible():
    rx = works("user: Alice, order 42, id 9f2k1x", "user: Alice, order 42, id q8w7e6")
    assert not re.compile(rx).search("order 42")                    # the order number is not masked, only the changing id


def test_no_suggestion_when_nothing_can_be_generalised_or_it_would_not_work():
    assert suggest_mask("same", "same") is None
    assert suggest_mask("abc", "abcdef") is None                    # one side only added text
    assert suggest_mask(5, 6) is None and suggest_mask(None, "x") is None


def test_the_suggested_mask_makes_two_whole_requests_match_through_the_real_normaliser():
    a = {"messages": [{"role": "user", "content": "Ticket TK-1042: where is order 42?"}]}
    b = {"messages": [{"role": "user", "content": "Ticket TK-2977: where is order 42?"}]}
    (_, x, y), = json_diff(a, b, raw=True)
    rx = suggest_mask(x, y)
    masks = compile_masks([rx])
    assert normalize(a, masks) == normalize(b, masks)
    assert normalize(a, compile_masks([])) != normalize(b, compile_masks([]))


def test_an_agent_that_cannot_start_is_reported_as_that_not_as_an_unstable_replay():
    from crosstrace.runner import AgentSpec, RunOutcome, not_started
    spec = AgentSpec(command=["python", "agent.py"])
    out = lambda code, err, t=False: RunOutcome(code, "", err, 0.1, t)
    msg = not_started(spec, out(1, "Traceback...\nModuleNotFoundError: No module named 'openai'"), [])
    assert "No module named 'openai'" in msg and "package the agent needs is missing" in msg and "python agent.py" in msg
    assert "could not start" in not_started(spec, out(127, "sh: 1: python: not found"), [])
    assert "crashed before making any model call" in not_started(spec, out(1, "ValueError: bad"), [])
    assert not_started(spec, out(1, "boom"), [{"provider": "openai"}]) is None          # it got as far as a model call: other checks apply
    assert not_started(spec, out(0, ""), []) is None and not_started(spec, out(1, "", True), []) is None
