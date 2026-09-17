"""微信开放平台网页登录（预留）。

平台配置 ``WECHAT_APP_ID`` 与 ``WECHAT_APP_SECRET`` 后自动启用扫码登录；
未配置时登录页仅展示用户名密码。登录流程遵循微信开放平台网站应用：
生成授权地址 -> 用户扫码授权 -> 回调携带 code -> 以 code 换 access_token 与 openid。
"""

import json
import os
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://open.weixin.qq.com/connect/qrconnect"
TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"


def app_id() -> str:
    return (os.getenv("WECHAT_APP_ID") or "").strip()


def app_secret() -> str:
    return (os.getenv("WECHAT_APP_SECRET") or "").strip()


def redirect_uri() -> str:
    return (os.getenv("WECHAT_REDIRECT_URI") or "").strip()


def enabled() -> bool:
    return bool(app_id() and app_secret())


def authorize_url(state: str) -> str:
    if not enabled():
        raise RuntimeError("未配置微信开放平台 AppID/AppSecret")
    params = {
        "appid": app_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "snsapi_login",
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params) + "#wechat_redirect"


def _get_json(url: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def exchange_code(code: str) -> dict:
    """用 code 换取微信身份，返回 openid/unionid/nickname/avatar。"""
    if not enabled():
        raise RuntimeError("未配置微信开放平台 AppID/AppSecret")
    query = urllib.parse.urlencode(
        {
            "appid": app_id(),
            "secret": app_secret(),
            "code": code,
            "grant_type": "authorization_code",
        }
    )
    token = _get_json(f"{TOKEN_URL}?{query}")
    if token.get("errcode"):
        raise RuntimeError(f"微信授权失败: {token.get('errmsg', token.get('errcode'))}")
    openid = token.get("openid", "")
    access_token = token.get("access_token", "")
    unionid = token.get("unionid")
    profile: dict = {}
    if access_token and openid:
        try:
            profile = _get_json(
                f"{USERINFO_URL}?"
                + urllib.parse.urlencode(
                    {"access_token": access_token, "openid": openid, "lang": "zh_CN"}
                )
            )
        except Exception:  # noqa: BLE001
            profile = {}
    return {
        "openid": openid,
        "unionid": unionid,
        "nickname": profile.get("nickname"),
        "avatar": profile.get("headimgurl"),
    }
