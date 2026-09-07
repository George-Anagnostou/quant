"""Bound API body reads before JSON decoding; reject ambiguous oversized inputs."""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class RequestLimitsMiddleware:
    def __init__(self, app, maximum=8*1024*1024):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.maximum:
                    raise HTTPException(413, detail={"code":"request_too_large", "message":"Request exceeds 8 MiB"})
            return message

        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.maximum+1
        if declared > self.maximum:
            response = JSONResponse({"detail":{"code":"request_too_large","message":"Request exceeds 8 MiB"}}, status_code=413)
            return await response(scope, receive, send)
        return await self.app(scope, bounded_receive, send)
