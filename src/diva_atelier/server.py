"""HTTP API and static frontend, standard library only.

    GET  /api/health
    GET  /api/configs                          PUT  /api/configs/<key>
    POST /api/configs/<key>/validate           POST /api/configs/<key>/reports
    POST /api/configs/<key>/feedback           GET  /api/configs/<key>/insights
    POST /api/configs/<key>/insights/<id>/apply
    GET  /api/reports                          GET  /api/reports/<id>
    GET  /api/reports/<id>/render?format=html|md
    POST /api/reports/<id>/approvals

Set DIVA_API_TOKEN to require ``Authorization: Bearer <token>`` on every /api route except health.
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agents import ApprovalError, GenerationError
from .llm import LLMError
from .service import AtelierService, ConfigInvalid, NotFound
from .store import StoreError

log = logging.getLogger("diva_atelier")
MAX_BODY = 1_000_000
FRONTEND = Path(os.environ.get("DIVA_FRONTEND_DIR") or Path(__file__).resolve().parents[2] / "frontend")


class BadRequest(ValueError):
    pass


def make_handler(service: AtelierService, token: str = "", frontend: Path = FRONTEND):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DivaAtelier/0.1"

        # ── plumbing ──
        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("DIVA_ACCESS_LOG"):
                super().log_message(fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise BadRequest("Request body too large.")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                raise BadRequest(f"Body is not valid JSON: {exc.msg}.") from exc
            if not isinstance(data, dict):
                raise BadRequest("Body must be a JSON object.")
            return data

        def _authorised(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got, f"Bearer {token}")

        def _dispatch(self, method: str) -> None:
            if method not in ("GET", "HEAD", "POST", "PUT"):
                self.send_response(405)
                self.send_header("Allow", "GET, HEAD, POST, PUT")
                body = b'{"error": "Method not allowed."}'
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            url = urlparse(self.path)
            path, query = url.path.rstrip("/") or "/", parse_qs(url.query)
            try:
                if path.startswith("/api"):
                    if path != "/api/health" and not self._authorised():
                        return self._json(401, {"error": "Missing or invalid bearer token."})
                    return self._api(method, path, query)
                if method in ("GET", "HEAD"):
                    return self._static(path)
                self._json(405, {"error": "Method not allowed."})
            except BadRequest as exc:
                self._json(400, {"error": str(exc)})
            except ConfigInvalid as exc:
                self._json(422, {"error": str(exc), "issues": exc.issues})
            except GenerationError as exc:
                self._json(422, {"error": str(exc), "problems": exc.problems})
            except NotFound as exc:
                self._json(404, {"error": str(exc)})
            except (ApprovalError, StoreError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
            except LLMError as exc:
                self._json(502, {"error": str(exc)})
            except Exception:                                     # log the traceback, never send it to clients
                log.exception("unhandled error handling %s %s", method, self.path)
                self._json(500, {"error": "Internal server error."})

        do_GET = lambda self: self._dispatch("GET")       # noqa: E731
        do_HEAD = lambda self: self._dispatch("HEAD")     # noqa: E731
        do_POST = lambda self: self._dispatch("POST")     # noqa: E731
        do_PUT = lambda self: self._dispatch("PUT")       # noqa: E731
        do_DELETE = lambda self: self._dispatch("DELETE")  # noqa: E731
        do_PATCH = lambda self: self._dispatch("PATCH")    # noqa: E731

        # ── static files ──
        def _static(self, path: str) -> None:
            rel = "index.html" if path == "/" else path.lstrip("/")
            target = (frontend / rel).resolve()
            if frontend.resolve() not in target.parents or not target.is_file():
                return self._json(404, {"error": "Not found."})
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        # ── API ──
        def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
            svc = service
            parts = path.split("/")[2:]               # after /api
            if parts == ["health"] and method == "GET":
                return self._json(200, {"status": "ok", "llm": svc.llm.name})
            if parts == ["configs"] and method == "GET":
                return self._json(200, svc.store.list_configs())
            if len(parts) >= 2 and parts[0] == "configs":
                key = parts[1]
                rest = parts[2:]
                if not rest and method == "PUT":
                    return self._json(200, {"issues": svc.save_config(key, self._body())})
                if not rest and method == "GET":
                    cfg = svc.store.get_config(key)
                    if cfg is None:
                        raise NotFound(f"No configuration '{key}'.")
                    return self._json(200, cfg)
                if rest == ["validate"] and method == "POST":
                    return self._json(200, {"issues": svc.validate(self._body())})
                if rest == ["reports"] and method == "POST":
                    b = self._body()
                    variables = b.get("variables")
                    if not isinstance(variables, dict):
                        raise BadRequest("'variables' must be an object.")
                    report = svc.generate(key, variables, author=str(b.get("author", "system")),
                                          auto_approve=bool(b.get("autoApprove", False)))
                    return self._json(201, report)
                if rest == ["feedback"] and method == "POST":
                    return self._json(201, svc.add_feedback(key, self._body()))
                if rest == ["insights"] and method == "GET":
                    return self._json(200, svc.insights(key))
                if len(rest) == 3 and rest[0] == "insights" and rest[2] == "apply" and method == "POST":
                    if not re.fullmatch(r"rec-[0-9a-f]{8}", rest[1]):
                        raise BadRequest("Invalid recommendation id.")
                    return self._json(200, svc.apply_insight(key, rest[1]))
            if parts == ["reports"] and method == "GET":
                return self._json(200, svc.store.list_reports())
            if len(parts) >= 2 and parts[0] == "reports":
                rid, rest = parts[1], parts[2:]
                if not rest and method == "GET":
                    return self._json(200, svc.get_report(rid))
                if rest == ["render"] and method == "GET":
                    body, ctype = svc.render(rid, (query.get("format") or ["html"])[0])
                    return self._send(200, body.encode(), ctype)
                if rest == ["approvals"] and method == "POST":
                    b = self._body()
                    missing = [k for k in ("requestId", "user", "role", "action") if not b.get(k)]
                    if missing:
                        raise BadRequest(f"Missing field(s): {', '.join(missing)}.")
                    return self._json(200, svc.decide(rid, b["requestId"], b["user"], b["role"],
                                                      b["action"], b.get("comment", "")))
            self._json(404, {"error": "Not found."})

    return Handler


def create_server(service: AtelierService, host: str = "127.0.0.1", port: int = 8000,
                  token: str | None = None, frontend: Path = FRONTEND) -> ThreadingHTTPServer:
    token = os.environ.get("DIVA_API_TOKEN", "") if token is None else token
    return ThreadingHTTPServer((host, port), make_handler(service, token, frontend))
