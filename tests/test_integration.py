import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from diva_atelier import cli
from diva_atelier.llm import LLMError, MockLLM, get_llm
from diva_atelier.llm.anthropic_client import AnthropicClient
from diva_atelier.retrieval import load_knowledge
from diva_atelier.server import create_server
from diva_atelier.service import AtelierService
from diva_atelier.store import JsonStore, StoreError
from helpers import ROOT, load_config, load_feedback, load_vars


def call(base, method, path, body=None, token=None, raw=False):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode()),
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            return r.status, (data.decode() if raw else json.loads(data)), r.headers
    except urllib.error.HTTPError as e:
        data = e.read()
        return e.code, (data.decode() if raw else json.loads(data)), e.headers


class ServerTests(unittest.TestCase):
    token = "s3cret"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        store = JsonStore(cls.tmp.name)
        for k in ("bridal", "evening"):
            store.save_config(k, load_config(k))
        for item in load_feedback():
            store.add_feedback("bridal", item)
        cls.service = AtelierService(store, load_knowledge(ROOT / "data" / "knowledge"), MockLLM())
        cls.srv = create_server(cls.service, "127.0.0.1", 0, token=cls.token, frontend=ROOT / "frontend")
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def api(self, method, path, body=None, **kw):
        return call(self.base, method, path, body, token=self.token, **kw)

    def test_health_is_open_but_api_needs_the_token(self):
        self.assertEqual(call(self.base, "GET", "/api/health")[0], 200)
        self.assertEqual(call(self.base, "GET", "/api/configs")[0], 401)
        self.assertEqual(call(self.base, "GET", "/api/configs", token="wrong")[0], 401)
        self.assertEqual(self.api("GET", "/api/configs")[0], 200)

    def test_frontend_is_served_and_cannot_escape_its_directory(self):
        status, body, headers = call(self.base, "GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("Diva Atelier", body)
        self.assertIn("text/html", headers["Content-Type"])
        for evil in ("/../src/diva_atelier/server.py", "/%2e%2e/pyproject.toml", "/..%2f..%2fetc/passwd"):
            self.assertEqual(call(self.base, "GET", evil)[0], 404, evil)

    def test_configs_roundtrip_and_validation_errors_are_422(self):
        s, cfgs, _ = self.api("GET", "/api/configs")
        self.assertEqual(set(cfgs), {"bridal", "evening"})
        bad = load_config("evening")
        bad["sections"][0]["parent"] = "ghost"
        s, body, _ = self.api("PUT", "/api/configs/evening", bad)
        self.assertEqual(s, 422)
        self.assertTrue(any(i["severity"] == "error" for i in body["issues"]))
        self.assertEqual(self.api("GET", "/api/configs/evening")[1]["sections"][0]["parent"], "")    # unchanged
        s, body, _ = self.api("POST", "/api/configs/evening/validate", load_config("evening"))
        self.assertEqual((s, body["issues"]), (200, []))
        self.assertEqual(self.api("GET", "/api/configs/nope")[0], 404)

    def test_generate_render_and_approve_a_report(self):
        s, rep, _ = self.api("POST", "/api/configs/evening/reports",
                             {"variables": load_vars("evening"), "author": "priya", "autoApprove": True})
        self.assertEqual(s, 201)
        self.assertEqual(rep["status"], "ready_for_review")
        rid = rep["id"]
        self.assertEqual(self.api("GET", f"/api/reports/{rid}")[1]["id"], rid)
        self.assertIn(rid, [r["id"] for r in self.api("GET", "/api/reports")[1]])
        s, html, h = self.api("GET", f"/api/reports/{rid}/render?format=html", raw=True)
        self.assertEqual(s, 200)
        self.assertIn("<h1>Monthly Evening Gown Review</h1>", html)
        self.assertEqual(self.api("GET", f"/api/reports/{rid}/render?format=md", raw=True)[0], 200)
        self.assertEqual(self.api("GET", f"/api/reports/{rid}/render?format=pdf", raw=True)[0], 400)

        wf = rep["workflow"]
        sec = [r for r in wf["requests"] if r["scope"] == "section"]
        self.assertTrue(all(r["status"] == "auto_approved" for r in sec))
        glob = [r for r in wf["requests"] if r["scope"] == "global"]
        s, body, _ = self.api("POST", f"/api/reports/{rid}/approvals",
                              {"requestId": glob[0]["id"], "user": "priya", "role": glob[0]["role"], "action": "approve"})
        self.assertEqual(s, 400)                                      # authors cannot approve their own report
        for g, who in zip(glob, ("mark", "qc", "dana")):
            s, body, _ = self.api("POST", f"/api/reports/{rid}/approvals",
                                  {"requestId": g["id"], "user": who, "role": g["role"] if g["role"] != "*" else "Any", "action": "approve"})
            self.assertEqual(s, 200, body)
        self.assertEqual(body["status"], "approved")
        self.assertEqual(self.api("GET", "/api/reports/GR-DOESNOTEXIST")[0], 404)

    def test_bad_requests_get_clear_errors(self):
        self.assertEqual(self.api("POST", "/api/configs/evening/reports", {"variables": "x"})[0], 400)
        s, body, _ = self.api("POST", "/api/configs/evening/reports", {"variables": {}})
        self.assertEqual(s, 422)
        self.assertTrue(body["problems"])
        self.assertEqual(self.api("POST", "/api/configs/evening/validate", b"{not json")[0], 400)
        self.assertEqual(self.api("POST", "/api/configs/evening/validate", b"[1]")[0], 400)
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        conn.putrequest("POST", "/api/configs/evening/validate")
        conn.putheader("Authorization", f"Bearer {self.token}")
        conn.putheader("Content-Length", "5000000")                 # claimed size over the 1 MB cap
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertIn("too large", json.loads(resp.read())["error"])
        conn.close()
        self.assertEqual(self.api("GET", "/api/configs/..%2fsecrets")[0], 400)
        s, body, h = self.api("DELETE", "/api/configs/evening")
        self.assertEqual((s, h["Allow"]), (405, "GET, HEAD, POST, PUT"))
        self.assertEqual(self.api("GET", "/api/configs")[1].keys() >= {"evening"}, True)     # nothing was deleted
        self.assertEqual(self.api("GET", "/api/nothing")[0], 404)

    def test_feedback_insights_apply_loop(self):
        s, recs, _ = self.api("GET", "/api/configs/bridal/insights")
        self.assertEqual(s, 200)
        rec = next(r for r in recs if "cheap" in r["title"])
        self.assertEqual(self.api("POST", "/api/configs/bridal/insights/rec-zzzzzzzz/apply")[0], 400)
        self.assertEqual(self.api("POST", "/api/configs/bridal/insights/rec-00000000/apply")[0], 404)
        before_version = self.api("GET", "/api/configs/bridal")[1]["version"]
        s, updated, _ = self.api("POST", f"/api/configs/bridal/insights/{rec['id']}/apply")
        self.assertEqual((s, updated["version"]), (200, before_version + 1))
        _, recs_after, _ = self.api("GET", "/api/configs/bridal/insights")
        self.assertNotIn(rec["id"], [r["id"] for r in recs_after])         # consumed, not re-proposed
        # new feedback validation
        self.assertEqual(self.api("POST", "/api/configs/bridal/feedback", {"type": "note", "section": "ES"})[0], 400)
        self.assertEqual(self.api("POST", "/api/configs/bridal/feedback", {"type": "comment", "section": "ES"})[0], 400)
        s, fb, _ = self.api("POST", "/api/configs/bridal/feedback", {"type": "comment", "section": "ES", "text": "Nice."})
        self.assertEqual(s, 201)
        self.assertTrue(fb["id"])


class StoreTests(unittest.TestCase):
    def test_keys_are_restricted_and_writes_are_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            st = JsonStore(d)
            for bad in ("../x", "a/b", "", ".hidden", "x" * 80):
                with self.assertRaises(StoreError):
                    st.save_config(bad, {})
            st.save_config("ok-1", {"a": 1})
            self.assertEqual(st.get_config("ok-1"), {"a": 1})
            self.assertIsNone(st.get_config("missing"))
            self.assertFalse(list(Path(d, "configs").glob("*.tmp")))


class StubAnthropic(BaseHTTPRequestHandler):
    plan: list = []
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        StubAnthropic.seen.append((self.path, dict(self.headers), body))
        status, payload = StubAnthropic.plan.pop(0)
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class AnthropicClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), StubAnthropic)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def client(self, **kw):
        return AnthropicClient(api_key="k", base_url=self.url, backoff=0.01, **kw)

    def test_request_shape_and_response_parsing(self):
        StubAnthropic.seen.clear()
        StubAnthropic.plan = [(200, {"model": "m1", "content": [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}],
                                     "usage": {"input_tokens": 7, "output_tokens": 3}})]
        r = self.client().complete(system="sys", prompt="hi", max_tokens=50)
        self.assertEqual((r.text, r.model, r.input_tokens, r.output_tokens), ("Hello world", "m1", 7, 3))
        path, headers, body = StubAnthropic.seen[0]
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual((body["system"], body["messages"], body["max_tokens"]), ("sys", [{"role": "user", "content": "hi"}], 50))

    def test_retries_transient_errors_then_succeeds(self):
        StubAnthropic.plan = [(429, {"error": "slow down"}), (529, {"error": "overloaded"}),
                              (200, {"content": [{"type": "text", "text": "ok"}]})]
        self.assertEqual(self.client().complete(system="s", prompt="p").text, "ok")

    def test_gives_up_after_max_retries_and_does_not_retry_client_errors(self):
        StubAnthropic.plan = [(503, {"error": "x"})] * 3
        with self.assertRaisesRegex(LLMError, "503"):
            self.client(max_retries=2).complete(system="s", prompt="p")
        StubAnthropic.plan = [(400, {"error": "bad"}), (200, {"content": []})]
        with self.assertRaisesRegex(LLMError, "400"):
            self.client().complete(system="s", prompt="p")
        self.assertEqual(len(StubAnthropic.plan), 1)                    # the 400 was not retried

    def test_missing_key_and_provider_selection(self):
        import os
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(LLMError):
                AnthropicClient()
            with self.assertRaises(LLMError):
                get_llm("nonsense")
            self.assertEqual(get_llm("mock").name, "mock")
        finally:
            if old:
                os.environ["ANTHROPIC_API_KEY"] = old


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_validate_exit_codes(self):
        self.assertEqual(self.run_cli("validate", str(ROOT / "configs" / "bridal.json"))[0], 0)
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d, "bad.json")
            cfg = load_config("bridal")
            cfg["sections"][0]["parent"] = "ghost"
            bad.write_text(json.dumps(cfg))
            code, out, _ = self.run_cli("validate", str(bad))
            self.assertEqual(code, 1)
            self.assertIn("INVALID", out)

    def test_generate_writes_each_format(self):
        with tempfile.TemporaryDirectory() as d:
            for fmt, needle in (("md", "# Bridal Collection Report"), ("html", "<h1>"), ("json", '"config_id": "C-0041"')):
                out = Path(d, f"r.{fmt}")
                code, msg, _ = self.run_cli("generate", str(ROOT / "configs" / "bridal.json"),
                                            "--vars", str(ROOT / "examples" / "bridal_vars.json"),
                                            "--knowledge", str(ROOT / "data" / "knowledge"), "--format", fmt, "--out", str(out))
                self.assertEqual(code, 0, msg)
                self.assertIn(needle, out.read_text(encoding="utf-8"))

    def test_generate_with_bad_inputs_exits_2_with_reasons(self):
        with tempfile.TemporaryDirectory() as d:
            v = Path(d, "v.json")
            v.write_text("{}")
            code, _, err = self.run_cli("generate", str(ROOT / "configs" / "bridal.json"), "--vars", str(v))
            self.assertEqual(code, 2)
            self.assertIn("is required", err)

    def test_insights_prints_json(self):
        code, out, _ = self.run_cli("insights", str(ROOT / "configs" / "bridal.json"),
                                    "--feedback", str(ROOT / "data" / "feedback" / "bridal.json"))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out))


if __name__ == "__main__":
    unittest.main()
