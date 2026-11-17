"""Suggest a [replay] mask for a value that changes on every run.

Replay matches a request by its normalised body, so one value that differs between two runs (a request id, a counter, a name made up per
run) makes every later request miss. The stability check sees the recorded value and the new one; from the pair this builds a regex and
checks that it really makes them equal before offering it, so a suggestion is never a guess that does nothing.
"""
import re

CONTEXT = 16                                    # characters before the changing part that the mask must see (kept short: lookbehind is fixed width)


def _shape(text):
    """A regex for the kind of characters in `text`: runs of digits, letters, or an id-like mix; punctuation stays literal."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", text) and re.search(r"\d", text) and re.search(r"[A-Za-z]", text):
        return r"[A-Za-z0-9_-]+"
    out, i = [], 0
    while i < len(text):
        c = text[i]
        kind = r"\d+" if c.isdigit() else r"[A-Za-z]+" if c.isalpha() else None
        if kind is None:
            out.append(re.escape(c))
            i += 1
            continue
        j = i
        while j < len(text) and (text[j].isdigit() if c.isdigit() else text[j].isalpha()):
            j += 1
        out.append(kind)
        i = j
    return "".join(out)


def _union(a, b):
    return a if a == b else f"(?:{a}|{b})"


def suggest_mask(old, new):
    """A regex that makes `old` and `new` equal once its matches are replaced by <masked>, or None when no safe one can be made."""
    if not isinstance(old, str) or not isinstance(new, str) or old == new:
        return None
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    s = 0
    while s < min(len(old), len(new)) - p and old[len(old) - 1 - s] == new[len(new) - 1 - s]:
        s += 1
    a, b = old[p:len(old) - s], new[p:len(new) - s]
    if not a or not b:                          # one side only added text: nothing to generalise from
        return None
    before = old[max(0, p - CONTEXT):p]
    after = old[len(old) - s:len(old) - s + CONTEXT] if s else ""
    core = _union(_shape(a), _shape(b))
    rx = (f"(?<={re.escape(before)})" if before else "") + core + (f"(?={re.escape(after)})" if after else "")
    try:
        compiled = re.compile(rx)
    except re.error:
        return None
    if compiled.sub("<masked>", old) != compiled.sub("<masked>", new):
        return None
    if not compiled.search(old):
        return None
    return rx
