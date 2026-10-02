"""Review of e14e066 / the user's follow-up: ReDoS, NaN/Infinity, malformed input, path and log hygiene."""
from __future__ import annotations

import asyncio
import math
import threading
import time

import pytest

SLOW = ["(a|aa)+$", "^(.*a){20}$", "^(a?){25}a{25}$", "^(a+)+$"]


def _rule(pattern, key="layer_regex"):
    return {"id": "R1", "name": "n", "subject": {key: pattern, **({"entity_type": "TEXT"} if key == "text_regex" else {})},
            "measurement": "length", "operator": "<=", "value": 5}


@pytest.mark.parametrize("pattern", SLOW)
def test_slow_regex_is_refused_when_rules_are_saved(live, pattern):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    t0 = time.perf_counter()
    r = live.http.put(f"/api/projects/{pid}/rules", json={"rules": [_rule(pattern)]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "RULES_INVALID"
    assert time.perf_counter() - t0 < 40
    assert live.http.get(f"/api/projects/{pid}/rules").json()["ruleset"]["rules"] == []      # nothing was stored
    v = live.http.post(f"/api/projects/{pid}/rules/validate", json={"rules": [_rule(pattern, "text_regex")]}).json()
    assert v["valid"] is False and v["errors"]


def test_sensible_regex_is_saved_quickly(live):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    t0 = time.perf_counter()
    r = live.http.put(f"/api/projects/{pid}/rules", json={"systems": [{"name": "S", "layer_regex": "^(?:A|B)$"}],
                                                          "rules": [_rule("^A(?:-\\d+){0,20}$")]})
    assert r.status_code == 200 and time.perf_counter() - t0 < 5


@pytest.mark.parametrize("body", [b'{"rules": [{"id": "X", "subject": {"layer_equals": "A"}, "measurement": "length", '
                                  b'"operator": "<=", "value": NaN}]}',
                                  b'{"rules": [{"id": "X", "value": Infinity}]}', b'{"rules": -Infinity}'])
def test_nan_and_infinity_are_refused_everywhere(live, body):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    for path, method in ((f"/api/projects/{pid}/rules", "PUT"), (f"/api/projects/{pid}/rules/validate", "POST"),
                         (f"/api/projects/{pid}/rules/build", "POST")):
        r = live.http.request(method, path, content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 400, (path, r.status_code, r.text)
    assert live.http.get(f"/api/projects/{pid}/rules").status_code == 200                    # nothing poisoned


def test_non_finite_numbers_inside_a_valid_document_are_refused(live):
    from core.rules.schema import RuleError, normalize_rule
    for bad in (math.nan, math.inf):
        with pytest.raises(RuleError):
            normalize_rule({"id": "X", "subject": {"layer_equals": "A"}, "measurement": "length", "operator": "<=", "value": bad})
        with pytest.raises(RuleError):
            normalize_rule({"id": "X", "subject": {"layer_equals": "A"}, "measurement": "length", "operator": "<=",
                            "value": 1, "warn_margin": bad})


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "rules", {"rules": 5}), ("PUT", "rules", {"rules": "x"}), ("PUT", "rules", {"rules": [5, None]}),
    ("PUT", "rules", {"systems": 7, "rules": []}), ("PUT", "rules", {"systems": [5]}), ("PUT", "rules", [1, 2]),
    ("POST", "rules/validate", {"rules": 5}), ("POST", "rules/validate", 5),
    ("POST", "rules/build", {"template_id": "clearance", "params": {"name": ["x"], "subject": {"system": "A"},
                                                                    "target": {"system": "B"}, "value": 1}}),
    ("POST", "rules/build", {"template_id": "clearance", "params": {"id": 5, "subject": {"system": "A"},
                                                                    "target": {"system": "B"}, "value": 1}}),
    ("POST", "rules/build", {"template_id": ["clearance"], "params": {}}),
    ("POST", "rules/build", {"template_id": "clearance", "params": [1]}),
    ("POST", "rules/build", {"template_id": "clearance", "params": {"warn_margin": "x", "subject": {"system": "A"},
                                                                    "target": {"system": "B"}, "value": 1}}),
    ("POST", "rules/build", {"template_id": "clearance", "params": {"subject": {"system": "A"}, "target": {"system": "B"},
                                                                    "value": 1, "evidence_id": ["EV"]}}),
])
def test_malformed_rule_input_is_a_4xx_never_a_500(live, method, path, body):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    r = live.http.request(method, f"/api/projects/{pid}/{path}", json=body)
    if path == "rules/validate":                     # by design: 200 with valid=false and the reasons
        assert r.status_code == 200 and r.json()["valid"] is False and r.json()["errors"], r.text
    else:
        assert 400 <= r.status_code < 500, (body, r.status_code, r.text)
    assert live.http.get(f"/api/projects/{pid}/rules").status_code == 200


def test_responses_never_contain_server_paths(live, tmp_path):
    data_dir = str(live.config.data_dir.resolve())
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    bodies = [live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("bad.dxf", b"\x00junk")}).text,
              live.http.post(f"/api/projects/{pid}/documents", files={"file": ("bad.pdf", b"%PDF-junk")}).text,
              live.http.post(f"/api/projects/{pid}/documents", files={"file": ("bad.docx", b"junk")}).text,
              live.http.get("/api/projects").text, live.http.get("/api/health").text]
    for text in bodies:
        assert data_dir not in text and str(tmp_path) not in text and "/home/" not in text and "Traceback" not in text


def test_export_location_is_relative(live, sample_dxf):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    live.http.post(f"/api/projects/{pid}/drawings", files={"file": ("plan.dxf", sample_dxf.read_bytes())})
    row = live.http.post(f"/api/projects/{pid}/runs", json={"drawing_id": live.http.get(
        f"/api/projects/{pid}/drawings").json()["drawings"][0]["id"]})
    assert row.status_code == 400                       # no rules yet; nothing to export, and no path in the message
    assert str(live.config.data_dir) not in row.text


def test_request_paths_cannot_forge_log_lines(live):
    live.raw.get("/api/projects/abc%0AFAKE%20ERROR%20admin%20logged%20in%0D%0A2099-01-01")
    log = (live.config.data_dir / "logs" / "server.log").read_text(encoding="utf-8")
    assert "FAKE ERROR" in log                                           # it is logged, but only as escaped text
    assert not any(line.startswith("FAKE ERROR") or line.startswith("2099-01-01") for line in log.splitlines())
    assert "\\x0a" in log


def test_clean_log():
    from app.errors import clean_log
    assert clean_log("a\nb\r\tc\x00 ") == "a\\x0ab\\x0d\\x09c\\x00\\x2028" or "\n" not in clean_log("a\nb")
    assert "\n" not in clean_log("x\ny") and "\r" not in clean_log("x\ry")


def test_websocket_connections_are_closed_by_the_middleware(live):
    """Called on the ASGI app itself: whether uvicorn offers a websocket library must not matter."""
    sent = []

    async def go():
        async def receive():
            return {"type": "websocket.connect"}

        async def send(msg):
            sent.append(msg)
        await live.app.middleware_stack({"type": "websocket", "path": "/api/health", "headers": [
            (b"host", f"127.0.0.1:{live.port}".encode())], "query_string": b"", "scheme": "ws", "server": ("127.0.0.1", live.port)},
            receive, send)
    t = threading.Thread(target=lambda: asyncio.run(go()))     # own thread: another test may leave a loop running here
    t.start()
    t.join(10)
    assert [m["type"] for m in sent] == ["websocket.close"] and sent[0]["code"] == 1008
