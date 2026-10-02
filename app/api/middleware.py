import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class WebhookBodyLimit:
    """Bound webhook bodies before FastAPI parses JSON, including chunked requests."""

    def __init__(self, app: ASGIApp, limit: int = 4096, body_timeout: float = 10) -> None:
        self.app, self.limit, self.body_timeout = app, limit, body_timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(
            "/api/v1/homeassistant/webhook/"
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            if not declared.isdigit():
                await JSONResponse({"detail": "Invalid request size"}, status_code=400)(
                    scope, receive, send
                )
                return
            if len(declared) > 20 or int(declared) > self.limit:
                await JSONResponse({"detail": "Request too large"}, status_code=413)(
                    scope, receive, send
                )
                return
        body = bytearray()
        try:
            async with asyncio.timeout(self.body_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.limit:
                        await JSONResponse({"detail": "Request too large"}, status_code=413)(
                            scope, receive, send
                        )
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "Request timed out"}, status_code=408)(
                scope, receive, send
            )
            return
        consumed = False

        async def bounded_receive() -> Message:
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)
