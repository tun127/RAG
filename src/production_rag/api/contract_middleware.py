"""Bound contract request bodies before multipart spooling; local-demo origin guard."""

from fastapi import Request
from fastapi.responses import JSONResponse


class ContractBoundaryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        contract_path = (
            path == "/contracts"
            or path.startswith("/contracts/")
            or path == "/api/contracts"
            or path.startswith("/api/contracts/")
            or path == "/product/upload"
            or path.startswith("/product/upload/")
        )
        if scope["type"] != "http" or not contract_path:
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                    ),
                ]
            await send(message)

        async def reject(status, message):
            if path.startswith("/contracts/"):
                from production_rag.api.routes.contracts import view

                response = view(Request(scope), error=message)
                response.status_code = status
            else:
                response = JSONResponse({"detail": message}, status_code=status)
            return await response(scope, receive, secure_send)

        if scope["method"] == "POST":
            headers = {k.lower(): v for k, v in scope["headers"]}
            origin = headers.get(b"origin")
            host = headers.get(b"host", b"")
            expected_origin = scope["scheme"].encode() + b"://" + host
            if origin is not None and origin != expected_origin:
                return await reject(403, "拒绝跨站上传")
            # Also handles chunked bodies and false Content-Length. Multipart overhead is bounded.
            limit = 512 * 1024 + 16 * 1024
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > limit:
                    return await reject(413, "上传请求超过当前试验限制（文件 512 KiB）")
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            replayed = False

            async def bounded_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, bounded_receive, secure_send)
        return await self.app(scope, receive, secure_send)
