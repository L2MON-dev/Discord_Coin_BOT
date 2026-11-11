from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import ssl
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web
from discord.utils import escape_markdown

from .. import config
from ..views.base import quietly
from ..views.deposit import deposit_complete_view
from .bank_sms import (
    KST, account_matches, is_deposit_kind, is_payment_stop, is_reversal_kind, looks_like_deposit,
    looks_like_reversal, mentions_account, parse_kb_alert,
)

if TYPE_CHECKING:
    from ..bot import CoinBot
    from ..services.deposit_service import IncomingTransfer

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
MAX_NAME_LENGTH = 50
MAX_SMS_LENGTH = 1000
MAX_AMOUNT = 1_000_000_000_000
# 한 주소에서 이만큼 인증에 실패하면 그 뒤 요청은 읽지 않고 거절한다
BAD_AUTH_LIMIT = 10
BAD_AUTH_WINDOW_SECONDS = 60
# 인증 봇이 응답을 기다리고 있으므로 역할 부여(디스코드 호출)는 이 시간까지만 기다린다
LINK_ROLE_TIMEOUT_SECONDS = 8
# Cloudflare 터널을 거친 요청에 붙는 헤더. 터널 요청도 127.0.0.1에서 온다
_PROXY_HEADERS = ("CF-Connecting-IP", "CF-Ray", "CF-Visitor", "X-Forwarded-For")


def link_signature_for(timestamp: str, body: bytes, secret: str) -> str:
    """인증 봇이 X-Signature에 넣는 값. X-Timestamp(유닉스 초), 줄바꿈, 본문을 이어 VERIFY_LINK_SECRET으로 HMAC-SHA256 (hex)."""
    return hmac.new(secret.encode(), timestamp.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def _link_signature_matches(request: web.Request, body: bytes) -> bool:
    timestamp = (request.headers.get("X-Timestamp") or "").strip()
    signature = (request.headers.get("X-Signature") or "").strip().lower()
    if not signature or not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
        return False
    if abs(time.time() - int(timestamp)) > config.WEBHOOK_MAX_SKEW_SECONDS:
        return False
    expected = link_signature_for(timestamp, body, config.VERIFY_LINK_SECRET)
    return hmac.compare_digest(expected.encode(), signature.encode())


def _snowflake(value: object) -> int | None:
    # 숫자(또는 숫자 문자열)로 온 디스코드 id만 받는다
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        number = int(value)
        return number if 0 < number < 2 ** 63 else None
    return None


def signature_for(timestamp: str, text: str, secret: str) -> str:
    """단축어가 X-Signature에 넣는 값. 타임스탬프, 문자 내용, 시크릿을 한 줄씩 이어 SHA-256.

    시크릿을 맨 뒤에 둬서, 가로챈 서명을 다른 메시지로 늘려 쓰지 못하게 한다.
    """
    return hashlib.sha256(f"{timestamp}\n{text}\n{secret}".encode()).hexdigest()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _from_this_machine(request: web.Request) -> bool:
    """터널을 거치지 않고 이 서버 안에서 온 요청인지."""
    if any(header in request.headers for header in _PROXY_HEADERS):
        return False
    return _is_loopback(request.remote or "")


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _client_ip(request: web.Request) -> str:
    # 터널 뒤에서는 모든 요청이 127.0.0.1로 보인다
    return request.headers.get("CF-Connecting-IP") or request.remote or "?"


def _key_matches(key: object) -> bool:
    return isinstance(key, str) and hmac.compare_digest(key.encode(), config.WEBHOOK_SECRET.encode())


_ISO_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_timestamp(value: str) -> float | None:
    """X-Timestamp를 epoch 초로 바꾼다. 유닉스 초와 ISO 8601(단축어의 날짜 형식)을 받고, 시간대가 없으면 한국 시간으로 본다."""
    value = value.strip()
    if value.isdigit():
        return float(value)
    normalized = _ISO_OFFSET_RE.sub(r"\1:\2", value.replace("Z", "+00:00").replace(" ", "T"))
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    return moment.timestamp()


def _signature_matches(request: web.Request, data: dict) -> bool:
    timestamp = (request.headers.get("X-Timestamp") or "").strip()
    signature = (request.headers.get("X-Signature") or "").strip().lower()
    text = data.get("text")
    if not timestamp or not signature or not isinstance(text, str):
        return False
    moment = parse_timestamp(timestamp)
    if moment is None or abs(time.time() - moment) > config.WEBHOOK_MAX_SKEW_SECONDS:
        return False
    expected = signature_for(timestamp, text, config.WEBHOOK_SECRET)
    return hmac.compare_digest(expected.encode(), signature.encode())


def _has_signature(request: web.Request) -> bool:
    return "X-Signature" in request.headers or "X-Timestamp" in request.headers


def _authenticate(request: web.Request, data: dict) -> str | None:
    """단축어가 보낸 요청이 맞으면 None, 아니면 그 이유."""
    if _has_signature(request):
        return None if _signature_matches(request, data) else "bad signature"
    if config.WEBHOOK_SIGNED:
        return "signature required"
    return None if _key_matches(data.get("key")) else "bad key"


def _sent_over_http(request: web.Request) -> bool:
    """폰이 Cloudflare까지 http://로 보냈는지 (키가 암호화 없이 나간 경우).

    Cloudflare가 CF-Visitor나 X-Forwarded-Proto로 알려 준다. 둘 다 없으면(터널 없음) 알 수 없다.
    """
    try:
        scheme = json.loads(request.headers.get("CF-Visitor", "{}")).get("scheme")
    except (ValueError, AttributeError):
        scheme = None
    scheme = scheme or request.headers.get("X-Forwarded-Proto")
    return isinstance(scheme, str) and scheme.lower() == "http"


class WebhookServer:
    def __init__(self, bot: CoinBot) -> None:
        self._bot = bot
        self._app = web.Application(client_max_size=MAX_BODY_BYTES)
        self._runner: web.AppRunner | None = None
        # IP -> 최근 인증 실패 시각들
        self._bad_auth: dict[str, list[float]] = {}
        self._setup_routes()

    def _too_many_failures(self, ip: str) -> bool:
        now = time.monotonic()
        for key in [k for k, times in self._bad_auth.items()
                    if now - times[-1] >= BAD_AUTH_WINDOW_SECONDS]:
            del self._bad_auth[key]
        recent = [t for t in self._bad_auth.get(ip, []) if now - t < BAD_AUTH_WINDOW_SECONDS]
        return len(recent) >= BAD_AUTH_LIMIT

    def _note_failure(self, ip: str) -> None:
        now = time.monotonic()
        times = [t for t in self._bad_auth.get(ip, []) if now - t < BAD_AUTH_WINDOW_SECONDS]
        times.append(now)
        self._bad_auth[ip] = times

    def _setup_routes(self) -> None:
        self._app.router.add_post("/api/deposit", self._handle_deposit)
        self._app.router.add_post("/api/verified", self._handle_verified)
        self._app.router.add_get("/api/health", self._handle_health)

    async def start(self) -> None:
        ssl_context: ssl.SSLContext | None = None
        if config.API_SSL_CERT:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(config.API_SSL_CERT, config.API_SSL_KEY)
        elif not _is_loopback(config.API_HOST):
            log.warning(
                "Webhook listens on %s over plain HTTP, so the secret is sent unencrypted. "
                "Use API_HOST=127.0.0.1 behind an HTTPS tunnel, or set API_SSL_CERT/API_SSL_KEY.",
                config.API_HOST,
            )

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, config.API_HOST, config.API_PORT, ssl_context=ssl_context
        )
        await site.start()
        log.info(
            "Webhook server started on %s:%s (%s)",
            config.API_HOST, config.API_PORT, "https" if ssl_context else "http",
        )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            log.info("Webhook server stopped")

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_verified(self, request: web.Request) -> web.Response:
        """인증 봇이 인증을 마친 유저를 알려 온다. 그 유저에게 입금자명, 이용 권한, 입금자 역할을 준다.

        VERIFY_LINK_SECRET을 가진 이 서버 안의 호출만 받는다. 터널은 /api/deposit과 /api/health만
        넘기지만, 그래도 터널로 들어온 요청에는 없는 주소라고 답한다.
        """
        if not config.VERIFY_LINK_SECRET or not _from_this_machine(request):
            return _error("not found", 404)
        body = await request.read()
        if not _link_signature_matches(request, body):
            log.warning("Rejected a verification report: bad signature")
            await self._bot.events.record(
                "webhook.bad_link",
                "서명이 맞지 않는 본인인증 완료 알림을 거절했어요. 인증 봇과 코인 봇의 연동 키"
                "(COIN_BOT_LINK_SECRET · VERIFY_LINK_SECRET)가 서로 다르거나, 이 서버의 다른 "
                "프로그램이 시도하는 중일 수 있어요.\n"
                "-# 그동안 인증을 마친 유저는 입금자명이 자동으로 지정되지 않아요",
                level="warning",
                dedup="webhook.bad_link",
            )
            return _error("unauthorized", 403)
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):  # JSON이 아니거나 UTF-8이 아니거나 지나치게 깊은 중첩
            data = None
        if not isinstance(data, dict):
            data = {}
        user_id = _snowflake(data.get("discord_id"))
        name, digits = data.get("name"), data.get("phone_last4")
        if (
            user_id is None or not isinstance(name, str) or not isinstance(digits, str)
            or len(name) > MAX_NAME_LENGTH or len(digits) > MAX_NAME_LENGTH
        ):
            log.warning("Rejected a verification report: unreadable fields")
            return _error("invalid report", 400)

        result = await self._bot.deposits.assign_verified_name(user_id, name, digits)
        if result.status in ("assigned", "exists"):
            try:
                await asyncio.wait_for(
                    self._bot.grant_depositor_role(user_id), LINK_ROLE_TIMEOUT_SECONDS
                )
            except Exception:
                # 이름은 지정됐고 역할만 빠진 상태
                log.warning("Giving the depositor role to %s failed", user_id, exc_info=True)
        log.info("Verification report: user=%s status=%s", user_id, result.status)
        return web.json_response({"status": result.status, "depositor_name": result.name})

    async def _handle_deposit(self, request: web.Request) -> web.Response:
        if not config.WEBHOOK_SECRET:
            return _error("webhook disabled", 503)
        ip = _client_ip(request)
        if self._too_many_failures(ip):
            return _error("too many attempts", 429)

        try:
            data = await request.json()
        except Exception:
            data = None
        if _sent_over_http(request):
            # 키가 맞아도 거절한다. 단축어 주소가 http://로 잘못 들어간 걸 바로 알 수 있게
            await self._refuse_http(request, data)
            return _error("https required", 403)
        if not isinstance(data, dict):
            await self._bad_request("JSON 형식이 아닌 입금 알림 요청을 거절했어요.", request)
            return _error("invalid json", 400)

        refused = _authenticate(request, data)
        if refused is not None:
            self._note_failure(ip)
            log.warning("Rejected deposit webhook from %s: %s", ip, refused)
            if refused == "signature required":
                why = ("서명 없이(본문 key만으로) 들어온 입금 알림을 거절했어요. WEBHOOK_SIGNED=true라 "
                       "단축어가 X-Timestamp·X-Signature 헤더를 보내야 해요.")
            elif refused == "bad signature":
                why = ("서명이 맞지 않는 입금 알림을 거절했어요. 단축어의 서명 계산이나 폰 시계가 "
                       "틀렸거나, 누군가 주소를 알아내 시도하는 중일 수 있어요.")
            else:
                why = "잘못된 키로 들어온 입금 알림 요청을 거절했어요. 단축어의 key 값이 바뀌었거나, 누군가 주소를 알아내 시도하는 중일 수 있어요."
            await self._bot.events.record(
                "webhook.bad_key", f"{why}\n-# IP `{ip}`", level="warning", dedup="webhook.bad_key",
            )
            return _error("unauthorized", 403)
        self._bot.last_webhook_at = datetime.now(timezone.utc)

        # 은행 문자 원문만 받는다. 이름과 금액만 받으면 키가 샜을 때 아무 금액이나 충전할 수 있고
        # 계좌·시각·중복 검사도 건너뛰게 된다
        raw_text = data.get("text")
        if not isinstance(raw_text, str) or len(raw_text) > MAX_SMS_LENGTH:
            await self._bad_request(
                "입금 문자 원문(text)이 없는 입금 알림을 거절했어요. 입금자명·금액만 보내는 방식은 "
                "받지 않아요 · 단축어가 문자 내용을 그대로 보내는지 확인하세요.",
                request,
            )
            return _error("invalid text", 400)
        sms = parse_kb_alert(raw_text)
        reversal_unread = False
        if sms is not None and is_reversal_kind(sms.kind):
            if " ".join(sms.name.split()) and 0 < sms.amount <= MAX_AMOUNT:
                return await self._reversal(sms)
            reversal_unread = True  # 이름이나 금액을 못 읽었으면 사람이 본다
        if sms is None or not is_deposit_kind(sms.kind):
            # 출금 등 다른 알림은 무시한다. 읽지 못한 문자는 내용에 따라 관리자에게 알린다
            quoted = raw_text.replace("`", "'")[:500]
            if reversal_unread or (sms is None and looks_like_reversal(raw_text)):
                log.warning("Could not parse a deposit reversal SMS")
                paused = await self._pause_if_stop(sms, raw_text)
                await self._bot.events.record(
                    "deposit.sms_unparsed",
                    "입금 취소·반환·지급정지와 관련된 것 같은 문자를 해석하지 못했어요. 은행 앱에서 무슨 "
                    "일인지 먼저 확인하세요. 충전해 준 돈이 빠져나간 경우에만 `/관리자 이용정지`로 송금을 "
                    f"막고 `/관리자 잔액차감`으로 바로잡아요.\n```\n{quoted}\n```" + paused,
                    level="error",
                )
            elif looks_like_deposit(raw_text):
                log.warning("Could not parse a deposit SMS")
                await self._bot.events.record(
                    "deposit.sms_unparsed",
                    "입금 문자를 해석하지 못해 자동 충전하지 못했어요. 문자를 확인하고 "
                    f"`/관리자 잔액충전`으로 직접 충전하세요.\n```\n{quoted}\n```",
                    level="warning",
                )
            return web.json_response({"ignored": True})
        if config.BANK_ACCOUNT and not account_matches(sms.account, config.BANK_ACCOUNT):
            # 우리 계좌가 아니다. 위조 문자이거나 단축어가 다른 계좌 문자까지 보내는 경우
            await self._wrong_account(sms, request)
            return web.json_response({"ignored": True, "reason": "account"})

        name = " ".join(sms.name.split())
        if not name or len(name) > MAX_NAME_LENGTH:
            await self._bad_request("입금자명이 비었거나 너무 긴 입금 알림을 거절했어요.", request)
            return _error("invalid name", 400)
        if not 0 < sms.amount <= MAX_AMOUNT:
            await self._bad_request(
                f"금액을 읽을 수 없는 입금 알림을 거절했어요 (받은 값 `{sms.amount}`).", request
            )
            return _error("invalid amount", 400)

        log.info("Incoming deposit notification: name=%r amount=%d", name, sms.amount)

        result = await self._bot.deposits.process_incoming(
            name, sms.amount, sent_at=sms.sent_at, sms_key=sms.key
        )
        notification_id = result.notification.id

        if result.duplicate:
            # 이미 받은 문자
            return web.json_response({"duplicate": True, "notification_id": notification_id})
        if result.held:
            # 수동 충전으로 이미 받은 돈일 수 있어 충전하지 않고 관리자에게 넘겼다
            # 문자는 저장됐으니 여기서부터는 응답이 실패하면 안 된다 (단축어가 다시 보낸다)
            if result.deposit is not None:
                await quietly(
                    self._bot.deposit_prompts.checking(result.deposit.id), "Updating the deposit prompt"
                )
            return web.json_response({"matched": False, "held": True, "notification_id": notification_id})
        if result.deposit is None:
            # 맞지 않은 이유는 process_incoming이 이미 기록했다
            log.info("No matching deposit: notification=%s", notification_id)
            return web.json_response({"matched": False, "notification_id": notification_id})

        await quietly(self._notify_user(result), "Telling the user about a credited deposit")
        return web.json_response({"matched": True, "notification_id": notification_id})

    async def _reversal(self, sms) -> web.Response:
        # 입금 취소·반환 문자: 관리자에게 알리기만 하고 잔액은 그대로 둔다
        if config.BANK_ACCOUNT and not account_matches(sms.account, config.BANK_ACCOUNT):
            log.info("Ignored a reversal SMS for another account (%s)", sms.account)
            return web.json_response({"ignored": True, "reason": "account"})
        name = " ".join(sms.name.split())[:MAX_NAME_LENGTH]
        stored, duplicate = await self._bot.deposits.record_reversal(
            name, sms.amount, sms.kind, sent_at=sms.sent_at, sms_key=sms.key
        )
        if stored is None:
            # 경고는 나갔지만 문자를 저장하지 못했다. 오류로 답해 단축어가 다시 보내게 한다
            return _error("reversal not stored", 503)
        return web.json_response({
            "ignored": True, "reason": "reversal", "notification_id": stored.id, "duplicate": duplicate,
        })

    async def _pause_if_stop(self, sms, raw_text: str) -> str:
        """우리 계좌의 지급정지 문자면 다 읽지 못했어도 충전·송금을 멈춘다 (계좌의 돈이 묶였을 수 있다).

        단어만 들어간 안내 문자(은행의 피싱 주의 문자 등)는 우리 계좌번호가 없으므로 멈추지 않는다.
        예외를 올리지 않고, 경고에 붙일 한 줄을 돌려준다.
        """
        if sms is not None:  # 취소 계열 문자인데 이름이나 금액을 못 읽은 경우
            ours = not config.BANK_ACCOUNT or account_matches(sms.account, config.BANK_ACCOUNT)
            stop = ours and is_payment_stop(sms.kind)
        else:
            stop = (
                bool(config.BANK_ACCOUNT) and mentions_account(raw_text, config.BANK_ACCOUNT)
                and any(is_payment_stop(line) for line in raw_text.splitlines())
            )
        if not stop:
            return ""
        return "\n" + await self._bot.deposits.pause_for_stop("해석하지 못한 지급정지 문자")

    async def _refuse_http(self, request: web.Request, data: object) -> None:
        log.warning("Rejected deposit webhook sent over plain http from %s", _client_ip(request))
        ours = isinstance(data, dict) and (
            _key_matches(data.get("key")) or (_has_signature(request) and _signature_matches(request, data))
        )
        if not ours:
            return  # 우리 단축어가 아니면 서버 로그로 충분하다
        await self._bot.events.record(
            "webhook.insecure",
            f"암호화되지 않은 `http://` 주소로 들어온 입금 알림을 거절했어요 (IP `{_client_ip(request)}`).\n"
            "단축어의 주소를 `https://`로 바꾸세요. 키가 암호화 없이 전송됐으니 웹훅 키(WEBHOOK_SECRET)도 "
            "새로 만들어 서버와 단축어에 넣는 게 안전해요. 이 알림의 입금은 처리하지 않았으니, "
            "들어온 돈이 있으면 은행 앱에서 확인한 뒤 `/관리자 잔액충전`으로 충전하세요.",
            level="error",
            dedup="webhook.insecure",
        )

    async def _wrong_account(self, sms, request: web.Request) -> None:
        log.warning("Ignored a deposit SMS for another account (%s)", sms.account)
        await self._bot.events.record(
            "deposit.wrong_account",
            "입금 계좌가 설정한 계좌(BANK_ACCOUNT)와 다른 입금 문자라 충전하지 않았어요 · "
            f"문자 속 계좌 `{sms.account}` · **{escape_markdown(sms.name)}** {sms.amount:,}원\n"
            "위조 문자이거나, 단축어가 다른 계좌의 문자까지 보내고 있는 경우예요. 은행 앱에서 "
            "실제로 들어온 돈인지 확인하고, 맞다면 `/관리자 잔액충전`으로 충전하세요.",
            level="error",
            dedup=f"wrong_account:{sms.key}",
            details={"IP": _client_ip(request)},
        )

    async def _bad_request(self, message: str, request: web.Request) -> None:
        await self._bot.events.record(
            "webhook.invalid",
            f"{message}\n-# 단축어 설정을 확인하세요 · IP `{_client_ip(request)}`",
            level="warning",
            dedup="webhook.invalid",
        )

    async def _notify_user(self, result: IncomingTransfer) -> None:
        deposit = result.deposit
        await self._bot.notify_user(
            deposit.discord_id, view=deposit_complete_view(deposit.amount, result.new_balance)
        )
        await self._bot.deposit_prompts.complete(deposit.id, deposit.amount, result.new_balance)
