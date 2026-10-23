"""
Каналы доставки сигнала врачу. Новый канал = новый класс в REGISTRY, остальной код не меняется.
Секреты (токен бота, почта) — только из переменных окружения.
В мессенджер и почту НЕ уходят ФИО и диагноз (152-ФЗ) — только метка пациента и ссылка.
"""
import json
import logging
import os
import urllib.request

from django.core.mail import send_mail

log = logging.getLogger("alerts")


class Channel:
    name = "base"

    def recipient(self, user):
        return ""

    def send(self, alert, user):  # при ошибке — исключение
        raise NotImplementedError

    @staticmethod
    def text(alert):
        base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
        link = f"{base}{alert.card_url}" if alert.card_url else f"{base}/alerts/"
        return f"[{alert.get_severity_display()}] {alert.title}\nПациент: {alert.patient_label}\nОткрыть: {link}"


class InApp(Channel):
    """В кабинете врача: сигнал появляется в ленте (страница /alerts/ и API /api/alerts/)."""

    name = "inapp"

    def send(self, alert, user):
        return None


class Log(Channel):
    name = "log"

    def send(self, alert, user):
        log.warning("ALERT %s -> user %s: %s", alert.id, user.id, alert.title)


class Email(Channel):
    name = "email"

    def recipient(self, user):
        r = getattr(user, "alert_recipient", None)
        return (r.email if r and r.email else "") or user.email

    def send(self, alert, user):
        to = self.recipient(user)
        if not to:
            raise RuntimeError("у врача не указан e-mail")
        send_mail(f"Лямбда: {alert.title}", self.text(alert), None, [to], fail_silently=False)


class Telegram(Channel):
    name = "telegram"

    def recipient(self, user):
        r = getattr(user, "alert_recipient", None)
        return r.telegram_chat_id if r else ""

    def send(self, alert, user):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat = self.recipient(user)
        if not token or not chat:
            raise RuntimeError("не задан TELEGRAM_BOT_TOKEN или chat_id врача")
        body = json.dumps({"chat_id": chat, "text": self.text(alert)}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()


REGISTRY = {c.name: c for c in (InApp, Log, Email, Telegram)}

# Срочные — во все включённые каналы; важные — кабинет и мессенджер; «к сведению» — только кабинет
BY_SEVERITY = {"critical": None, "high": {"inapp", "log", "telegram"}, "medium": {"inapp"}}
