"""Epic launcher authorization-code flow, as used by Legendary/Heroic."""
from __future__ import annotations

import re
import json
import asyncio
import fcntl
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urlparse

import httpx

from app.secure_store import normalize_email, replace_native_credential, load_task_context, get_account, save_native_account

# Public launcher OAuth client identifiers from Legendary's EPCAPI.
CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
ACCOUNT_API = "https://account-public-service-prod03.ol.epicgames.com/account/api"
STORE_URL = "https://store.epicgames.com/en-US/free-games"


class NativeAuthError(RuntimeError):
    """A sanitized authentication failure; never include upstream responses."""

    def __init__(self, message: str, error_type: str = "native_auth_expired", stage: str = "oauth"):
        super().__init__(message)
        self.error_type = error_type
        self.stage = stage


def login_url() -> str:
    redirect = "https://www.epicgames.com/id/api/redirect?" + urlencode(
        {"clientId": CLIENT_ID, "responseType": "code"}
    )
    return "https://www.epicgames.com/id/login?" + urlencode({"redirectUrl": redirect})


def parse_authorization_code(value: str) -> str:
    value = value.strip()
    if value.startswith("{"):
        try:
            value = json.loads(value).get("authorizationCode")
        except (ValueError, AttributeError):
            raise ValueError("请粘贴 Epic 的 authorizationCode 或完整 JSON") from None
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", value):
        raise ValueError("需要 Epic 的一次性授权码，不能填写验证器的 6 位验证码")
    return value


async def _json(client: httpx.AsyncClient, method: str, url: str, *, stage: str, **kwargs) -> dict:
    try:
        response = await client.request(method, url, **kwargs)
        if response.status_code >= 400:
            # Only Epic's explicit invalid-token response establishes expiry.
            try:
                code = response.json().get("errorCode", "")
            except (ValueError, AttributeError):
                code = ""
            expired = code in {
                "errors.com.epicgames.account.auth_token.invalid_refresh_token",
                "errors.com.epicgames.account.oauth.authorization_code_not_found",
                "errors.com.epicgames.account.oauth.exchange_code_not_found",
                "errors.com.epicgames.account.auth_token.invalid_token",
            }
            raise NativeAuthError(
                "Epic 登录凭据已失效，请重新进行官方登录" if expired else "Epic 授权服务未完成请求，请稍后重试",
                "native_auth_expired" if expired else "native_auth_failed", stage,
            )
        data = response.json()
        if not isinstance(data, dict) or data.get("errorCode"):
            raise NativeAuthError("Epic 未返回有效的授权结果，请重新登录", "native_auth_failed", stage)
        return data
    except (httpx.HTTPError, ValueError):
        raise NativeAuthError("无法连接 Epic 授权服务，请稍后重试", "network_timeout", stage) from None


async def _session(client: httpx.AsyncClient, grant: dict) -> dict:
    data = await _json(client, "POST", ACCOUNT_API + "/oauth/token",
                       stage=grant["grant_type"],
                       data={**grant, "token_type": "eg1"},
                       auth=httpx.BasicAuth(CLIENT_ID, CLIENT_SECRET))
    if not all(isinstance(data.get(k), str) and data[k]
               for k in ("account_id", "access_token", "refresh_token")):
        raise NativeAuthError("Epic 未返回可刷新的登录凭据，请重新授权")
    return {k: data[k] for k in ("account_id", "access_token", "refresh_token",
                                "expires_at", "refresh_expires_at") if k in data}


async def authorize(code: str, client: httpx.AsyncClient) -> tuple[str, dict]:
    session = await _session(client, {"grant_type": "authorization_code", "code": code})
    account = await _json(client, "GET", ACCOUNT_API + "/public/account/" + session["account_id"],
                          stage="account_identity",
                          headers={"Authorization": "Bearer " + session["access_token"]})
    if account.get("id") != session["account_id"]:
        raise NativeAuthError("Epic 账号身份校验失败，请重新登录")
    try:
        email = normalize_email(account.get("email", ""))
    except (ValueError, AttributeError):
        raise NativeAuthError("无法读取 Epic 账号邮箱，请检查账号信息") from None
    return email, session


async def refresh(session: dict, client: httpx.AsyncClient) -> dict:
    updated = await _session(client, {"grant_type": "refresh_token", "refresh_token": session["refresh_token"]})
    if updated["account_id"] != session["account_id"]:
        raise NativeAuthError("Epic 账号身份发生变化，请重新授权")
    return updated


async def web_exchange_url(session: dict, client: httpx.AsyncClient) -> str:
    data = await _json(client, "GET", ACCOUNT_API + "/oauth/exchange",
                       stage="web_exchange",
                       headers={"Authorization": "Bearer " + session["access_token"]})
    code = data.get("code", "")
    if not isinstance(code, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", code):
        raise NativeAuthError("无法建立 Epic 网页登录会话，请重新授权")
    return "https://www.epicgames.com/id/exchange?" + urlencode(
        {"exchangeCode": code, "redirectUrl": STORE_URL}
    )


async def bootstrap_browser(page, session: dict, client: httpx.AsyncClient,
                            db_path: str, run_id: str, cipher) -> None:
    async with session_lock(db_path, session["account_id"]):
        current = json.loads(load_task_context(db_path, cipher, run_id).password)
        updated = await refresh(current, client)
        replace_native_credential(db_path, cipher, run_id, json.dumps(updated))
    url = await web_exchange_url(updated, client)
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response and response.status >= 400:
            destination = urlparse(response.url)
            surface = "exchange" if destination.path == "/id/exchange" else "store" if destination.hostname == "store.epicgames.com" else "other"
            raise NativeAuthError(f"Epic 网页拒绝请求 HTTP {response.status} surface={surface}，请稍后重试",
                                  "native_web_session_failed", "web_document")
        # Epic's exchange page is a SPA: DOMContentLoaded precedes its async
        # /id/api/exchange request. Navigating away here cancels cookie creation.
        await page.wait_for_url(
            lambda value: urlparse(str(value)).hostname == "store.epicgames.com"
            or (urlparse(str(value)).hostname == "www.epicgames.com"
                and urlparse(str(value)).path.startswith("/id/correction/")),
            wait_until="domcontentloaded", timeout=45000,
        )
    except NativeAuthError:
        raise
    except Exception as exc:
        # Playwright navigation exceptions contain the URL and its exchange code.
        if type(exc).__name__ == "TargetClosedError":
            raise RuntimeError("target page, context or browser has been closed") from None
        raise NativeAuthError("Epic 网页登录未完成跳转，浏览器会话尚未建立，请稍后重试",
                              "native_web_session_failed", "web_redirect") from None


@asynccontextmanager
async def session_lock(db_path: str, account_id: str):
    """Serialize refresh-token rotation across web and worker on shared storage."""
    import hashlib
    folder = Path(db_path).parent / "oauth_locks"
    folder.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(folder / hashlib.sha256(account_id.encode()).hexdigest(), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + 45
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise NativeAuthError("账号授权正在更新，请稍后重试", "network_timeout", "refresh_lock")
                await asyncio.sleep(0.1)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


async def account_library(db_path: str, cipher, email: str, client: httpx.AsyncClient) -> list[dict]:
    row = get_account(db_path, email)
    if not row or row["auth_method"] != "epic_oauth":
        raise NativeAuthError("请先使用 Epic 官方登录连接账号", stage="account_identity")
    initial = json.loads(cipher.decrypt(row["credential_ciphertext"]))

    async def current_session(rejected_token=None):
        async with session_lock(db_path, initial["account_id"]):
            session = json.loads(cipher.decrypt(get_account(db_path, email)["credential_ciphertext"]))
            if session["account_id"] != initial["account_id"]:
                raise NativeAuthError("账号已切换，请重新加载库存", "native_auth_failed", "account_identity")
            try:
                usable = datetime.fromisoformat(session.get("expires_at", "").replace("Z", "+00:00")) > datetime.now(timezone.utc) + timedelta(seconds=60)
            except (ValueError, TypeError):
                usable = False
            if not usable or rejected_token == session["access_token"]:
                session = await refresh(session, client)
                save_native_account(db_path, cipher, email, session)
            return session

    session = await current_session()
    try:
        return await list_library(session, client)
    except NativeAuthError as exc:
        if exc.error_type != "native_auth_expired":
            raise
        session = await current_session(session["access_token"])
        return await list_library(session, client)


async def list_library(session: dict, client: httpx.AsyncClient) -> list[dict]:
    headers = {"Authorization": "Bearer " + session["access_token"]}
    records, seen, cursor = [], set(), None
    while True:
        params = {"includeMetadata": "true"}
        if cursor:
            params["cursor"] = cursor
        data = await _json(client, "GET", "https://library-service.live.use1a.on.epicgames.com/library/api/public/items",
                           stage="inventory", params=params, headers=headers)
        if not isinstance(data.get("records"), list):
            raise NativeAuthError("Epic 库存返回格式异常", "native_auth_failed", "inventory")
        records.extend(data["records"])
        cursor = data.get("responseMetadata", {}).get("nextCursor")
        if not cursor:
            break
        if cursor in seen:
            raise NativeAuthError("Epic 库存分页未完成，请稍后重试", "native_auth_failed", "inventory")
        seen.add(cursor)
    groups = {}
    for item in records:
        if item.get("namespace") == "ue" or item.get("sandboxType") == "PRIVATE":
            continue
        if item.get("namespace") and item.get("catalogItemId") and item.get("appName"):
            groups.setdefault(item["namespace"], {})[item["catalogItemId"]] = item
    semaphore = asyncio.Semaphore(8)

    async def catalog_games(namespace, items, ids):
        async with semaphore:
            catalog = await _json(client, "GET",
                "https://catalog-public-service-prod06.ol.epicgames.com/catalog/api/shared/namespace/" + namespace + "/bulk/items",
                stage="inventory_metadata", params={"id": ",".join(ids), "locale": "zh-CN", "includeMainGameDetails": "true"}, headers=headers)
        games = []
        for item_id in ids:
            meta = catalog.get(item_id, {})
            if meta.get("mainGameItem") or any(c.get("path") == "mods" for c in meta.get("categories", [])):
                continue
            image = next((i.get("url", "") for i in meta.get("keyImages", []) if i.get("type") in {"DieselGameBox", "DieselGameBoxTall", "Thumbnail"}), "")
            games.append({"id": namespace + ":" + item_id, "game": meta.get("title") or items[item_id]["appName"],
                          "image": image, "time": "Epic 账号库存"})
        return games

    requests = []
    for namespace, items in groups.items():
        ids = list(items)
        for start in range(0, len(ids), 50):
            requests.append(catalog_games(namespace, items, ids[start:start+50]))
    batches = await asyncio.gather(*requests)
    return [game for batch in batches for game in batch]
