"""알림과 생존 신호. 실패해도 매매 루프를 절대 멈추지 않습니다.

- Telegram: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 가 둘 다 있을 때만 전송합니다.
- Healthcheck ping: HEALTHCHECK_URL (예: healthchecks.io 무료 플랜)로 주기적으로 GET 합니다.
  봇이나 서버가 통째로 꺼지면 핑이 끊겨 외부 서비스가 알림을 보냅니다.
  프로세스 내부 알림만으로는 서버 자체가 죽은 경우를 알 수 없기 때문에 필요합니다.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import requests

from logger import get_logger

logger = get_logger()

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    """기본 구현은 아무것도 보내지 않는 no-op 입니다."""

    enabled = False

    def notify(self, text: str, *, key: str | None = None, cooldown: float = 0.0) -> None:
        return None

    def notify_sync(self, text: str) -> None:
        """프로세스가 곧 끝날 때 쓰는 동기 전송. 기본 구현은 아무것도 하지 않습니다."""
        return None

    def ping(self) -> None:
        return None


class HttpNotifier(Notifier):
    def __init__(
        self,
        telegram_token: str = "",
        telegram_chat_id: str = "",
        healthcheck_url: str = "",
        *,
        ping_interval: float = 60.0,
        timeout: float = 5.0,
        prefix: str = "[btc-bot]",
        post: Callable[..., object] | None = None,
        get: Callable[..., object] | None = None,
        background: bool = True,
    ):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.healthcheck_url = healthcheck_url
        self.ping_interval = ping_interval
        self.timeout = timeout
        self.prefix = prefix
        self._post = post or requests.post
        self._get = get or requests.get
        self._background = background
        self._last_sent: dict[str, float] = {}
        self._last_ping = float("-inf")
        self._lock = threading.Lock()
        self.enabled = bool(
            (telegram_token and telegram_chat_id) or healthcheck_url
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def notify(self, text: str, *, key: str | None = None, cooldown: float = 0.0) -> None:
        if not self.telegram_enabled:
            return
        now = time.monotonic()
        if key is not None and cooldown > 0:
            with self._lock:
                last = self._last_sent.get(key)
                if last is not None and now - last < cooldown:
                    return
                self._last_sent[key] = now
        message = f"{self.prefix} {text}"
        if self._background:
            threading.Thread(target=self._send, args=(message,), daemon=True).start()
        else:
            self._send(message)

    def notify_sync(self, text: str) -> None:
        if self.telegram_enabled:
            self._send(f"{self.prefix} {text}")

    def _send(self, message: str) -> None:
        try:
            self._post(
                TELEGRAM_URL.format(token=self.telegram_token),
                json={"chat_id": self.telegram_chat_id, "text": message[:3500]},
                timeout=self.timeout,
            )
        except Exception as exc:  # 알림 실패는 매매에 영향을 주면 안 됩니다.
            # 토큰이 URL 에 들어 있으므로 예외 문자열을 그대로 로그에 남기지 않습니다.
            logger.warning("텔레그램 알림 전송 실패 (%s)", type(exc).__name__)

    def ping(self) -> None:
        if not self.healthcheck_url:
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last_ping < self.ping_interval:
                return
            self._last_ping = now
        if self._background:
            threading.Thread(target=self._ping_once, daemon=True).start()
        else:
            self._ping_once()

    def _ping_once(self) -> None:
        try:
            self._get(self.healthcheck_url, timeout=self.timeout)
        except Exception as exc:
            logger.warning("헬스체크 핑 실패 (%s)", type(exc).__name__)


def build_notifier(cfg: object) -> Notifier:
    token = getattr(cfg, "telegram_bot_token", "")
    chat_id = getattr(cfg, "telegram_chat_id", "")
    url = getattr(cfg, "healthcheck_url", "")
    if not ((token and chat_id) or url):
        return Notifier()
    return HttpNotifier(token, chat_id, url)
