"""账号会话及可选的共享令牌鉴权。

仅当账号登录和共享令牌均未配置时放行。开启任一模式后，非公开路径要求有效的
账号会话或共享令牌；独立登录回调仍在各自端点验证其凭证。

令牌可以三种方式提供：

1. `Authorization: Bearer <token>` —— curl / 脚本 / 第三方集成
2. `X-Auth-Token: <token>` —— 同上，少打几个字
3. Cookie `videoinsight_token` —— 浏览器

**为什么必须有第 3 种**：媒体预览与下载是浏览器通过 `<video src>` / `<a href>`
发起的**原生请求**，这类请求没法附带自定义请求头。前端在用户填入令牌后调用
`POST /auth/session` 把它换成 HttpOnly Cookie，原生媒体请求才带得上鉴权。
少了这条通道，开启鉴权就等于直接打断媒体播放和下载。

用中间件而不是逐个路由加依赖：路由会越加越多，忘掉一个就是一个洞；中间件
按"默认拒绝 + 公开白名单"工作，新路由自动受保护。
"""
from __future__ import annotations

import logging
import secrets
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.account_auth import _owner, account_identity, login_required, session_account
from app.config import settings


LOGGER = logging.getLogger(__name__)

AUTH_COOKIE_NAME = "videoinsight_token"
# 浏览器端令牌有效期（秒）。比客户端身份 cookie 短得多：令牌是真正的凭据。
AUTH_COOKIE_MAX_AGE = 7 * 24 * 60 * 60
# 浏览器在数据请求上携带的账号身份标识，用于探测「账号已切换」的过期配置。
ACCOUNT_ID_HEADER = "X-VideoInsight-Account-Id"

# 必须匿名可达的路径：
# - "/" 与 /static：页面本身和它的 JS/CSS 得先加载出来，才能让用户输入令牌
# - /health：探活
# - /auth/session：它自己负责校验令牌并换成 Cookie
PUBLIC_PATHS = frozenset({
    "/", "/login", "/register", "/forgot-password", "/health", "/auth/session",
    "/auth/login", "/auth/logout", "/auth/me", "/auth/register", "/auth/register/code",
    "/auth/password/reset", "/auth/password/reset/code",
    "/cookies/helper/claim", "/cookies/helper/complete", "/cookies/helper/fail", "/cookies/helper/progress",
})
PUBLIC_PREFIXES = ("/static/", "/view/")

UNAUTHORIZED_DETAIL = "未授权：缺少或无效的访问令牌。"


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def extract_token(request: Request) -> str | None:
    """按 Bearer → X-Auth-Token → Cookie 的顺序取令牌。"""
    authorization = request.headers.get("authorization") or ""
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    header_token = (request.headers.get("x-auth-token") or "").strip()
    if header_token:
        return header_token
    cookie_token = (request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    return cookie_token or None


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    # 常量时间比较：普通 == 会在第一个不同字节处提前返回，理论上有计时侧信道。
    return secrets.compare_digest(provided.encode(), expected.encode())


async def auth_middleware(request: Request, call_next) -> Response:
    """未配置任何登录保护时放行，否则验证账号会话或共享令牌。"""
    trusted = {item.strip() for item in settings.auth_trusted_proxy_ips.split(",")}
    forwarded_scheme = request.headers.get("x-forwarded-proto")
    if request.client and request.client.host in trusted and forwarded_scheme in {"http", "https"}:
        request.scope["scheme"] = forwarded_scheme
    expected = settings.auth_token
    account_enabled = login_required()
    account = session_account(request)
    if account_enabled and (request.url.path in {"/", "/comments", "/account"} or request.url.path.startswith("/view/")) and not account:
        return RedirectResponse("/login", status_code=303)
    if expected is None and not account_enabled:
        return await call_next(request)
    if not (is_public_path(request.url.path) or account or (expected is not None and token_matches(extract_token(request), expected))):
        LOGGER.warning("拒绝未授权请求：%s %s", request.method, request.url.path)
        return JSONResponse(status_code=401, content={"detail": UNAUTHORIZED_DETAIL})
    path = request.url.path
    admin_only = path.startswith("/cookies/browser/")
    if account and not account.is_admin and admin_only:
        return JSONResponse(status_code=403, content={"detail": "普通账号请使用页内扫码或导入自己的 Cookies。"})
    # 拒绝携带过期账号身份的请求：同一浏览器切换账号后，旧账号的模型密钥等
    # 本地配置必须失效，不能让新账号继续沿用。处理在请求体解析之前完成。
    header_account = request.headers.get(ACCOUNT_ID_HEADER)
    if account_enabled and header_account and unquote(header_account) != account_identity(account):
        return JSONResponse(
            status_code=409,
            content={"code": "account_changed", "detail": "检测到账号已切换，请刷新页面后重新配置。"},
        )
    token = _owner.set(account.owner if account else None)
    try:
        return await call_next(request)
    finally:
        _owner.reset(token)
