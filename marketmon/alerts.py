"""Alert sinks (console, Telegram, email) and the P0-now / P1-digest dispatcher."""
from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

log = logging.getLogger("marketmon.alerts")
LABEL = {0: "P0", 1: "P1"}


@dataclass
class Alert:
    priority: int
    source: str
    headline: str
    url: str
    latency_s: float | None     # first_seen - published, None when the source gives no time
    cluster_id: int


def fmt_latency(s: float | None) -> str:
    if s is None:
        return "lag n/a"
    if s < 0:
        return "lag <0 (source clock)"
    return f"lag {s:.0f}s" if s < 120 else f"lag {s / 60:.1f}m"


def format_one(a: Alert) -> str:
    return f"[{LABEL[a.priority]}] {a.source.upper()} · {fmt_latency(a.latency_s)}\n{a.headline}\n{a.url}"


def format_digest(alerts: list[Alert]) -> str:
    lines = [f"[P1 digest] {len(alerts)} item(s)"]
    for a in alerts:
        lines.append(f"• {a.source.upper()} ({fmt_latency(a.latency_s)}): {a.headline}\n  {a.url}")
    return "\n".join(lines)


class ConsoleSink:
    name = "console"

    async def send(self, text: str) -> None:
        print("\n" + text, flush=True)


class TelegramSink:
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=15)

    async def send(self, text: str) -> None:
        r = await self.client.post(self.url, json={
            "chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True})
        r.raise_for_status()


class EmailSink:
    name = "email"

    def __init__(self, host, port, user, password, sender, to):
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.sender, self.to = sender, to

    def _send(self, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = text.splitlines()[0][:120]
        msg["From"], msg["To"] = self.sender, self.to
        msg.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)

    async def send(self, text: str) -> None:
        await asyncio.to_thread(self._send, text)


def build_sinks(names: list[str]) -> list:
    sinks = []
    env = os.environ.get
    for n in names:
        if n == "console":
            sinks.append(ConsoleSink())
        elif n == "telegram" and env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
            sinks.append(TelegramSink(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")))
        elif n == "email" and env("SMTP_HOST") and env("ALERT_EMAIL_TO"):
            sinks.append(EmailSink(env("SMTP_HOST"), env("SMTP_PORT", "587"), env("SMTP_USER", ""),
                                   env("SMTP_PASSWORD", ""), env("ALERT_EMAIL_FROM", env("SMTP_USER", "")),
                                   env("ALERT_EMAIL_TO")))
        elif n in ("telegram", "email"):
            log.info("%s sink listed but its env vars are not set; skipping", n)
    return sinks


class Dispatcher:
    """P0 goes out immediately; P1 is queued and sent as one digest every N minutes."""

    def __init__(self, sinks: list, store, digest_seconds: float = 300):
        self.sinks = sinks
        self.store = store
        self.digest_seconds = digest_seconds
        self.pending: list[Alert] = []

    async def submit(self, a: Alert) -> None:
        if a.priority == 0:
            await self._deliver(format_one(a), [a])
        else:
            self.pending.append(a)

    async def flush(self) -> None:
        batch, self.pending = self.pending, []
        if batch:
            await self._deliver(format_digest(batch), batch)

    async def digest_loop(self) -> None:
        while True:
            await asyncio.sleep(self.digest_seconds)
            await self.flush()

    async def _deliver(self, text: str, alerts: list[Alert]) -> None:
        ok = []
        for s in self.sinks:
            try:
                await s.send(text)
                ok.append(s.name)
            except Exception as e:   # one broken sink must not block the others
                log.warning("sink %s failed: %s", s.name, e)
                self.store.add_event("alerts", "sink_error", f"{s.name}: {e}")
        for a in alerts:
            self.store.add_alert(a.priority, a.source, a.cluster_id, a.headline, a.url,
                                 a.latency_s, ",".join(ok))
