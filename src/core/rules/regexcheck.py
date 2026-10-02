"""Save-time check for regular expressions that make matching explode (ReDoS).

Python's ``re`` cannot be given a timeout and cannot be interrupted from another thread, so safety
comes from three layers: (1) predicate.compile_regex rejects the classic nested-repeat shapes,
(2) this module *runs* every regex of a rule set against adversarial strings in a separate process
that is killed after a few seconds, and refuses the rule set when one never returns, and
(3) the job manager stops an analysis that stops reporting progress (see jobs.manager).
No heuristic can prove an arbitrary pattern safe; (2) catches the patterns that are slow on the
probe strings, not every conceivable one.
"""
from __future__ import annotations

import json
import subprocess
import sys

PROBE_SECONDS = 6.0
_SAFE: set[str] = set()      # patterns already probed in this process

_SCRIPT = r'''
import json, re, sys
pats = json.load(sys.stdin)
for i, p in enumerate(pats):
    print(i, flush=True)                         # the parent kills us while a pattern hangs: last index = culprit
    rx = re.compile(p, re.IGNORECASE)
    chars = list(dict.fromkeys([c for c in p if c.isalnum()][:6] + ["a", "0", " ", "-", "A"]))
    for c in chars:
        for n in (26, 34, 45):
            rx.search(c * n + "!")
            rx.search((c * 2) * (n // 2) + "!")
            rx.search((c + "-") * n + "!")
print("done", flush=True)
'''


def find_slow_patterns(patterns: list[str], timeout: float = PROBE_SECONDS) -> list[str]:
    """Patterns that did not finish the probe within ``timeout`` seconds each round."""
    todo = [p for p in dict.fromkeys(patterns) if p not in _SAFE]
    slow: list[str] = []
    while todo:
        try:
            done = subprocess.run([sys.executable, "-c", _SCRIPT], input=json.dumps(todo).encode(),
                                  capture_output=True, timeout=timeout)
            if done.returncode != 0:
                break                              # an invalid pattern: normalize_ruleset reports those
            _SAFE.update(todo)
            break
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode().split()
            idx = max((int(x) for x in out if x.isdigit()), default=0)
            _SAFE.update(todo[:idx])
            slow.append(todo[idx])
            todo = todo[idx + 1:]
    return slow


def _walk(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("layer_regex", "text_regex") and isinstance(v, str):
                out.append(v)
            elif k in ("and", "or") and isinstance(v, list):
                for c in v:
                    _walk(c, out)
            elif k == "not":
                _walk(v, out)


def collect_regexes(ruleset: dict) -> list[tuple[str, str]]:
    """(where, pattern) for every regular expression in a rule set."""
    found: list[tuple[str, str]] = []
    for s in ruleset.get("systems", []):
        if isinstance(s, dict) and isinstance(s.get("layer_regex"), str):
            found.append((f"系統「{s.get('name', '')}」", s["layer_regex"]))
    for r in ruleset.get("rules", []):
        if not isinstance(r, dict):
            continue
        for key in ("subject", "target", "zone"):
            pats: list[str] = []
            _walk(r.get(key), pats)
            found.extend((f"規則「{r.get('id', '')}」", p) for p in pats)
    return found


def check_ruleset(ruleset: dict) -> list[str]:
    """Readable error messages for patterns that hang the probe (empty when all is well)."""
    where = collect_regexes(ruleset)
    slow = set(find_slow_patterns([p for _, p in where]))
    return [f"{w}的比對規則 /{p}/ 可能讓分析卡住（巢狀或重疊的重複）。請簡化，或改用有上限的重複，例如 (…){{0,10}}。"
            for w, p in where if p in slow]
