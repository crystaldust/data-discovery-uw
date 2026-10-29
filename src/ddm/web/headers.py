"""Security headers on every response, as plain ASGI middleware (it must not buffer the
streamed CSV export or MCP responses)."""

import base64
import hashlib
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Pages use only same-origin CSS and JS (no inline script or style), so CSP stays 'self'.
# data: images are Pico's inline SVG icons (e.g. the select arrow).
PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; "
    "object-src 'none'"
)
# Swagger UI (served from /static) starts with one inline script, allowed by its hash
# only; it sets inline styles on its own elements.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' {script_hash}; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'"
)
INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)
COMMON = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
)
# Pages, exports and API answers come from a logged-in session or a key: keep them out of
# shared and browser caches. Static files may be cached.
NO_STORE = (b"cache-control", b"private, no-store")


def docs_csp(html: str) -> str:
    """The /docs policy for this exact page: its inline script's SHA-256, nothing looser."""
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
        for s in INLINE_SCRIPT.findall(html)
    ]
    return DOCS_CSP.format(script_hash=" ".join(hashes))


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, docs_policy: str = PAGE_CSP) -> None:
        self.app = app
        self.docs_policy = docs_policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        csp = self.docs_policy if path == "/docs" else PAGE_CSP
        extra = () if path.startswith("/static/") else (NO_STORE,)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"content-security-policy", b"cache-control")
                ]
                headers += [*COMMON, *extra, (b"content-security-policy", csp.encode())]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class McpPathMiddleware:
    """Serves /mcp as /mcp/: MCP clients are usually given the URL without the slash and
    should not have to follow a redirect (which may drop the Authorization header)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)
