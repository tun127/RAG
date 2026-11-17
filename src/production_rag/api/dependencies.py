import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request


def services(request: Request):
    return request.app.state.services


def authorize(request: Request, authorization: Annotated[str | None, Header()] = None):
    token = request.app.state.services.settings.api_token
    if token and not secrets.compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "无效的 API token")


def user_id(x_user_id: Annotated[str, Header(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = "demo"):
    # 本机演示身份，不是公网身份认证；部署网关必须覆盖此头。
    return x_user_id
