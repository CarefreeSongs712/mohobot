"""WebUI 路由沿用 Quart 风格的 ``request`` / ``jsonify`` / ``send_file``。

``api/*`` 里的处理器原本跑在 AstrBot 的 Quart 上；这里用 Starlette 提供同名
接口：每个请求进来时把适配后的请求对象放进 ContextVar，处理器里的全局
``request`` 代理按需读取，处理器本身无需改动。
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response


class QueryArgs(dict):
    """``request.args``：支持 ``get(key, default, type=int)``。"""

    def get(self, key: str, default: Any = None, type: Any = None) -> Any:  # noqa: A002
        if key not in self:
            return default
        value = self[key]
        if type is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class UploadedFile:
    """与 werkzeug FileStorage 一致的最小接口：filename / stream / read()。"""

    def __init__(self, filename: str, stream: Any) -> None:
        self.filename = filename
        self.stream = stream

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)


class RequestAdapter:
    def __init__(self, request: Request) -> None:
        self._request = request
        self.method = request.method.upper()
        self.args = QueryArgs(request.query_params)
        self.headers = request.headers
        self._json_loaded = False
        self._json: Any = None
        self._form_loaded = False
        self._form: dict[str, str] = {}
        self._files: dict[str, UploadedFile] = {}

    async def get_json(self, *_: Any, **__: Any) -> Any:
        """解析 JSON 请求体；不是 JSON 或解析失败时返回 None。"""
        if not self._json_loaded:
            self._json_loaded = True
            content_type = self._request.headers.get("content-type", "")
            if "multipart/form-data" in content_type:
                return None
            body = await self._request.body()
            if body:
                try:
                    self._json = json.loads(body)
                except (UnicodeDecodeError, ValueError):
                    self._json = None
        return self._json

    async def _load_form(self) -> None:
        if self._form_loaded:
            return
        self._form_loaded = True
        content_type = self._request.headers.get("content-type", "")
        if (
            "multipart/form-data" not in content_type
            and "application/x-www-form-urlencoded" not in content_type
        ):
            return
        form = await self._request.form()
        for key, value in form.multi_items():
            if isinstance(value, str):
                self._form.setdefault(key, value)
            else:
                self._files.setdefault(
                    key, UploadedFile(str(getattr(value, "filename", "") or ""), value.file)
                )

    async def _get_form(self) -> dict[str, str]:
        await self._load_form()
        return self._form

    async def _get_files(self) -> dict[str, UploadedFile]:
        await self._load_form()
        return self._files

    @property
    def form(self):
        """``await request.form``（与 Quart 一致是可等待属性）。"""
        return self._get_form()

    @property
    def files(self):
        """``await request.files``。"""
        return self._get_files()

    async def close(self) -> None:
        if self._form_loaded:
            for upload in self._files.values():
                try:
                    upload.stream.close()
                except Exception:
                    pass


_current_request: ContextVar[RequestAdapter | None] = ContextVar(
    "meme_stealer_request", default=None
)


class _RequestProxy:
    def __getattr__(self, name: str) -> Any:
        current = _current_request.get()
        if current is None:
            raise RuntimeError("request 只能在 WebUI 请求处理中使用")
        return getattr(current, name)


request = _RequestProxy()


def bind_request(adapter: RequestAdapter):
    """绑定当前请求，返回用于 ``reset_request`` 的 token。"""
    return _current_request.set(adapter)


def reset_request(token) -> None:
    _current_request.reset(token)


def jsonify(payload: Any = None, **kwargs: Any) -> JSONResponse:
    if payload is None and kwargs:
        payload = kwargs
    return JSONResponse(payload)


async def send_file(path: str | Path) -> FileResponse:
    return FileResponse(str(path))


def to_response(result: Any) -> Response:
    """处理器返回值 → Starlette 响应：支持 ``(response, status)`` 元组。"""
    status: int | None = None
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        result, status = result
    if isinstance(result, Response):
        response = result
    elif isinstance(result, (dict, list)):
        response = JSONResponse(result)
    elif isinstance(result, (str, bytes)):
        response = Response(result)
    elif result is None:
        response = Response(status_code=204)
    else:
        response = JSONResponse(result)
    if status is not None:
        response.status_code = status
    return response
