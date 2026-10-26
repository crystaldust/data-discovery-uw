"""Local web page for labelling eval/labels.csv (ddm label).

Standard library only. The server listens on 127.0.0.1 and nowhere else, and every request
must name a local host (blocks DNS rebinding) and carry the per-run token: the page in its
address, the API in an X-Label-Token header. Writes must be JSON, which a cross-site form
cannot send without a preflight this server never answers. Each label re-reads the file,
changes one row and saves atomically, so edits made elsewhere to other rows survive.
The page builds everything with textContent under a nonce-based CSP: feed titles are
untrusted text, never markup.
"""

import hmac
import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ddm.eval.labeling import load_pairs, save_pairs, with_label
from ddm.eval.sample import EvalInputError

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 4096
TOKEN_BYTES = 24
PAGE = "label_page.html"


class LabelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, labels_path: Path, port: int, token: str):
        super().__init__((HOST, port), LabelHandler)
        self.labels_path = labels_path
        self.token = token
        self.write_lock = threading.Lock()
        port = self.server_address[1]
        self.allowed_hosts = frozenset({f"{HOST}:{port}", f"localhost:{port}"})

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.server_address[1]}/?token={self.token}"


def make_server(labels_path: Path, *, port: int = DEFAULT_PORT, token: str | None = None):
    return LabelServer(labels_path, port, token or secrets.token_hex(TOKEN_BYTES))


def _page(token: str, nonce: str) -> bytes:
    template = files("ddm.eval").joinpath(PAGE).read_text(encoding="utf-8")
    # Both values are hex, so plain substitution cannot inject markup.
    return template.replace("{{TOKEN}}", token).replace("{{NONCE}}", nonce).encode("utf-8")


class LabelHandler(BaseHTTPRequestHandler):
    server: LabelServer
    # A client that announces a body and never sends it must not hold a thread forever.
    timeout = 10

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # The page address carries the token; keep request lines out of the console.
        logger.debug("label page request status=%s", args[1] if len(args) > 1 else "")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, headers=()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: object) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _trusted(self, token: str | None) -> bool:
        host_ok = self.headers.get("Host", "") in self.server.allowed_hosts
        return host_ok and token is not None and hmac.compare_digest(token, self.server.token)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        parts = urlsplit(self.path)
        if parts.path == "/":
            token = parse_qs(parts.query).get("token", [None])[0]
            if not self._trusted(token):
                return self._error(HTTPStatus.FORBIDDEN, "open the address ddm label printed")
            nonce = secrets.token_hex(16)
            policy = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
            headers = (("Content-Security-Policy", policy),)
            return self._send(
                HTTPStatus.OK, _page(self.server.token, nonce), "text/html; charset=utf-8", headers
            )
        if not self._trusted(self.headers.get("X-Label-Token")):
            return self._error(HTTPStatus.FORBIDDEN, "missing or wrong token")
        if parts.path != "/api/pairs":
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        try:
            labels = load_pairs(self.server.labels_path)
        except EvalInputError as exc:
            return self._error(HTTPStatus.CONFLICT, str(exc))
        self._json(HTTPStatus.OK, {"rows": [dict(row) for row in labels.rows]})

    def _read_label(self) -> tuple[str, str, str]:
        header = (self.headers.get("Content-Length") or "").strip()
        if not (header.isascii() and header.isdigit()):
            raise EvalInputError("send a valid Content-Length")
        length = int(header)
        if length > MAX_BODY_BYTES:
            raise OverflowError
        try:
            body = json.loads(self.rfile.read(length))
            return str(body["pair_id"]), str(body["label"]), str(body.get("note") or "")
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            raise EvalInputError("send JSON with pair_id, label and note") from exc

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if not self._trusted(self.headers.get("X-Label-Token")):
            return self._error(HTTPStatus.FORBIDDEN, "missing or wrong token")
        if urlsplit(self.path).path != "/api/label":
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send application/json")
        try:
            pair_id, label, note = self._read_label()
            with self.server.write_lock:
                path = self.server.labels_path
                updated = with_label(load_pairs(path), pair_id, label, note)
                save_pairs(path, updated)
        except OverflowError:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        except EvalInputError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        row = next(r for r in updated.rows if r["pair_id"] == pair_id)
        self._json(HTTPStatus.OK, {"row": dict(row)})
