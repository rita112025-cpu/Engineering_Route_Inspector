"""Security properties of the local server, checked against a real server on a real socket."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.config import AppConfig, ConfigError
from app.server import find_free_port, main

SRC = Path(__file__).resolve().parents[2] / "src"


# -- bind address ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com", "", "::", "127.0.0.1.evil.com"])
def test_config_refuses_any_non_loopback_host(tmp_path, host):
    with pytest.raises(ConfigError, match="只允許本機"):
        AppConfig(data_dir=tmp_path, host=host)


def test_default_host_is_loopback_and_cli_refuses_others(tmp_path, capsys):
    assert AppConfig(data_dir=tmp_path).host == "127.0.0.1"
    assert main(["--host", "0.0.0.0", "--data-dir", str(tmp_path), "--no-browser"]) == 2
    assert "只允許本機" in capsys.readouterr().err


def test_server_socket_is_bound_to_loopback_only(live):
    sockets = [s for srv in live.server.servers for s in srv.sockets]
    assert sockets and all(s.getsockname()[0] == "127.0.0.1" for s in sockets)


def test_cli_process_listens_only_on_loopback(tmp_path):
    import psutil
    port = find_free_port("127.0.0.1", 18900)
    proc = subprocess.Popen([sys.executable, "-m", "app", "--port", str(port), "--data-dir", str(tmp_path / "d"),
                             "--no-browser"], cwd=SRC, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        import time
        deadline = time.monotonic() + 30
        listening = []
        while time.monotonic() < deadline and not listening:
            listening = [c for c in psutil.Process(proc.pid).net_connections(kind="inet")
                         if c.status == psutil.CONN_LISTEN]
            time.sleep(0.1)
        assert listening, "server did not start"
        assert {c.laddr.ip for c in listening} == {"127.0.0.1"}
        assert httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=10).json()["status"] == "ok"
    finally:
        proc.terminate()
        proc.communicate(timeout=20)


# -- Host / Origin / token --------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1.evil.com:{port}", "evil.example:{port}",
                                  "localhost", "0.0.0.0:{port}", ""])
def test_wrong_host_header_is_rejected(live, host):
    r = live.raw.get("/api/health", headers={"Host": host.format(port=live.port)})
    assert r.status_code == 403 and r.json()["error"]["code"] == "BAD_HOST"


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}"])
def test_loopback_host_names_are_accepted(live, host):
    assert live.raw.get("/api/health", headers={"Host": host.format(port=live.port)}).status_code == 200


def test_foreign_origin_is_rejected_even_for_reads(live):
    r = live.http.get("/api/projects", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "BAD_ORIGIN"
    r = live.http.post("/api/projects", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = live.http.options("/api/projects", headers={"Origin": "http://evil.example",
                                                    "Access-Control-Request-Method": "POST"})
    assert r.status_code == 403
    assert live.http.get("/api/projects", headers={"Origin": live.base}).status_code == 200


def test_cross_site_fetch_metadata_is_rejected_for_changes(live):
    r = live.http.post("/api/projects", json={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = live.http.post("/api/projects", json={"name": "x"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 201


@pytest.mark.parametrize("method,url", [("POST", "/api/projects"), ("PATCH", "/api/projects/p_0123456789abcdef"),
                                         ("PUT", "/api/projects/p_0123456789abcdef/rules"),
                                         ("DELETE", "/api/projects/p_0123456789abcdef"),
                                         ("POST", "/api/runs/run_0123456789abcdef/cancel")])
def test_changes_need_the_token(live, method, url):
    r = live.raw.request(method, url, json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "BAD_TOKEN"
    r = live.raw.request(method, url, json={}, headers={"X-ERI-Token": "wrong"})
    assert r.status_code == 403
    r = live.raw.request(method, url, json={}, headers={"X-ERI-Token": live.token[:-1]})
    assert r.status_code == 403


def test_reads_do_not_need_the_token(live):
    assert live.raw.get("/api/projects").status_code == 200


def test_token_changes_with_every_start(tmp_path_factory):
    from tests.conftest import LiveServer
    a = LiveServer(tmp_path_factory.mktemp("a"))
    b = LiveServer(tmp_path_factory.mktemp("b"))
    try:
        assert a.token != b.token and len(a.token) >= 40
    finally:
        a.close()
        b.close()


def test_no_cors_headers_are_ever_sent(live):
    for path in ("/", "/api/health", "/api/projects"):
        r = live.http.get(path, headers={"Origin": live.base})
        assert not any(h.lower().startswith("access-control-") for h in r.headers)


def test_websocket_upgrade_is_refused(live):
    import socket
    s = socket.create_connection(("127.0.0.1", live.port), timeout=5)
    try:
        s.sendall((f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        reply = s.recv(4096).decode("latin-1")
    finally:
        s.close()
    assert "101" not in reply.split("\r\n")[0]


# -- headers --------------------------------------------------------------------------------------------------

def test_security_headers(live):
    api = live.http.get("/api/health")
    assert api.headers["x-content-type-options"] == "nosniff" and api.headers["cache-control"] == "no-store"
    assert api.headers["referrer-policy"] == "no-referrer" and api.headers["x-frame-options"] == "DENY"
    page = live.http.get("/")
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["cache-control"] == "no-store"
    assert live.token in page.text


# -- size limits -----------------------------------------------------------------------------------------------

def test_oversized_json_body_is_rejected(live):
    big = "x" * (live.config.max_json_kb * 1024 + 10)
    r = live.http.post("/api/projects", content=json.dumps({"name": big}).encode(),
                       headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "TOO_LARGE"


def test_streamed_body_without_content_length_is_cut_off(live):
    def chunks():
        for _ in range(live.config.max_json_kb + 8):
            yield b"x" * 1024
    r = live.http.post("/api/projects", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_oversized_upload_is_rejected_and_leaves_nothing_behind(live_factory):
    live = live_factory(max_document_mb=1)
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    data = b"a line of specification text\n" * 80000          # ~2.3 MB
    r = live.http.post(f"/api/projects/{pid}/documents", files={"file": ("spec.txt", data)})
    assert r.status_code == 413
    docs = live.config.data_dir / "projects" / pid / "documents"
    assert list(docs.iterdir()) == []
    ok = live.http.post(f"/api/projects/{pid}/documents", files={"file": ("small.txt", b"4.1 ok\nline\n")})
    assert ok.status_code == 201


# -- path traversal / identifiers ----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["..", "%2e%2e", "p_0123456789abcdeg", "p_short", "../etc", "%2e%2e%2fetc",
                                 "p_0123456789abcdef%00", "P_0123456789ABCDEF"])
def test_malformed_ids_are_404(live, bad):
    for path in (f"/api/projects/{bad}", f"/api/projects/{bad}/drawings", f"/api/runs/{bad}",
                 f"/api/exports/{bad}/download", f"/api/drawings/{bad}/geometry"):
        assert live.http.get(path).status_code in (404,), path


@pytest.mark.parametrize("path", ["/static/../../eri.sqlite3", "/static/%2e%2e/%2e%2e/eri.sqlite3",
                                  "/static/..%2f..%2fdata/eri.sqlite3", "/static//etc/passwd",
                                  "/static/%2e%2e%5c%2e%2e%5cwindows/win.ini"])
def test_static_files_cannot_escape_their_folder(live, path):
    r = live.http.get(path)
    assert r.status_code == 404 and b"SQLite" not in r.content and b"root:" not in r.content
    assert live.http.get("/static/app.js").status_code == 200


def test_upload_file_names_cannot_choose_the_destination(live):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    r = live.http.post(f"/api/projects/{pid}/documents",
                       files={"file": ("../../../../etc/evil.txt", b"4.1 text\nline\n")})
    assert r.status_code == 201
    stored = live.config.data_dir / "projects" / pid / "documents"
    names = [p.name for p in stored.iterdir()]
    assert len(names) == 1 and names[0].endswith("_evil.txt") and "/" not in names[0]
    assert not (live.config.data_dir.parent / "etc").exists()
    assert r.json()["filename"] == "evil.txt"


def test_tampered_export_path_is_refused(live):
    # a row pointing outside the export folder must never be served
    import sqlite3
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    conn = sqlite3.connect(live.state.storage.db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO runs(id, project_id, drawing_id, status, created_at, software_version) "
                 "VALUES ('run_aaaaaaaaaaaaaaaa', ?, 'd_aaaaaaaaaaaaaaaa', 'completed', 'now', 'x')", (pid,))
    conn.execute("INSERT INTO exports(id, run_id, format, path, sha256, created_at) VALUES "
                 "('exp_aaaaaaaaaaaaaaaa', 'run_aaaaaaaaaaaaaaaa', 'csv', 'projects/../../etc/passwd', 'x', 'now')")
    conn.commit()
    conn.close()
    r = live.http.get("/api/exports/exp_aaaaaaaaaaaaaaaa/download")
    assert r.status_code == 409 and "passwd" not in r.text


# -- error hygiene -------------------------------------------------------------------------------------------

def test_unexpected_errors_do_not_leak_details(live, monkeypatch):
    from persistence.repo import Repo

    def boom(self):
        raise RuntimeError("secret /home/user/data path and traceback")
    monkeypatch.setattr(Repo, "list_projects", boom)
    r = live.http.get("/api/projects")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in r.text and "Traceback" not in r.text and "RuntimeError" not in r.text
    log = (live.config.data_dir / "logs" / "server.log").read_text(encoding="utf-8")
    assert "RuntimeError" in log and "secret" in log          # the detail is kept for diagnostics only


def test_bad_uploads_give_readable_errors(live):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    up = lambda name, data: live.http.post(f"/api/projects/{pid}/drawings", files={"file": (name, data)})  # noqa: E731
    r = up("plan.dwg", b"AC1027....")
    assert r.status_code == 400 and r.json()["error"]["code"] == "DWG_UNSUPPORTED" and "DXF" in r.json()["error"]["message"]
    assert up("plan.dxf", b"AC1027" + b"\0" * 100).json()["error"]["code"] == "DWG_UNSUPPORTED"
    assert up("plan.png", b"x").json()["error"]["code"] == "NOT_DXF"
    r = up("plan.dxf", b"\x00\x01garbage")
    assert r.status_code == 400 and r.json()["error"]["code"] == "DRAWING_UNREADABLE"
    assert "Traceback" not in r.text
    r = live.http.post(f"/api/projects/{pid}/drawings", data={"x": "y"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "NO_FILE"
    assert list((live.config.data_dir / "projects" / pid / "drawings").iterdir()) == []   # rejected files are removed
    assert live.http.post(f"/api/projects/{pid}/documents", files={"file": ("a.exe", b"MZ")}).json()[
        "error"]["code"] == "DOC_UNSUPPORTED"
    assert live.http.post("/api/projects", content=b"{not json", headers={"Content-Type": "application/json"}
                          ).json()["error"]["code"] == "BAD_JSON"


def test_project_names_are_cleaned(live):
    for bad in ("", "   ", "x" * 101, None, 5):
        assert live.http.post("/api/projects", json={"name": bad}).status_code == 400
    p = live.http.post("/api/projects", json={"name": "  ‮A\u0000B  "}).json()
    assert p["name"] == "AB"


def test_server_makes_no_outbound_connections(live, monkeypatch):
    """Importing, analysing and exporting never open a network socket (checked at the socket layer)."""
    import socket
    calls = []
    real = socket.socket.connect

    def spy(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            calls.append(address)
        return real(self, address)
    monkeypatch.setattr(socket.socket, "connect", spy)
    live.http.get("/api/projects")
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    live.http.post(f"/api/projects/{pid}/documents", files={"file": ("s.txt", b"4.1 text\nline\n")})
    assert calls == []
