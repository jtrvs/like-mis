"""
Каналы доставки сигнала врачу. Новый канал = новый класс в REGISTRY, остальной код не меняется.
Секреты (токен бота, почта) — только из переменных окружения.
В мессенджер и почту НЕ уходят ФИО, диагноз и суть сигнала (152-ФЗ: сведения о здоровье) — только уровень,
метка пациента и ссылка в кабинет.
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
        # В сторонний мессенджер — только факт сигнала, метка и ссылка. Что именно случилось (это сведения о здоровье),
        # врач видит после входа в кабинет.
        base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
        kind = "срочное событие" if alert.severity in ("critical", "high") else "новое событие"
        return f"Лямбда: у пациента {alert.patient_label} {kind}. Откройте кабинет: {base}/alerts/#alert-{alert.id}"


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
        send_mail(f"Лямбда: новый сигнал «{alert.get_severity_display()}»", self.text(alert), None, [to], fail_silently=False)


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

# Красный — сразу в кабинет + вторым каналом (все включённые каналы); оранжевый — сразу в кабинет;
# жёлтый и «безопасность» — в карточку (сводка). Таблица правил, «Как выглядят категории».
BY_SEVERITY = {"critical": None, "high": {"inapp", "log"}, "medium": {"inapp"}, "safety": {"inapp"}}
