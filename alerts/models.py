"""
Сигналы (алерты) о критических состояниях пациента.

Модуль намеренно НЕ зависит от моделей пациента и врача, которые делает Юля:
- пациент хранится как его ID (patient_id) и короткая метка без ФИО (patient_label);
- врач — это обычный пользователь Django (User).
Так модуль можно подключить к любой модели пациента, ничего в ней не меняя.
"""
from django.conf import settings
from django.db import models


class Alert(models.Model):
    # Категории по таблице правил клинического психолога: красный, оранжевый, жёлтый и «безопасность терапии».
    # Коды оставлены прежними (critical/high/medium), чтобы не ломать данные и интеграции.
    SEVERITY = [("critical", "Экстренно"), ("high", "Срочно"), ("medium", "Внимание"), ("safety", "Безопасность")]
    SEVERITY_ORDER = {"critical": 0, "high": 1, "safety": 2, "medium": 3}
    STATUS = [
        ("new", "Создан"),
        ("delivered", "Доставлен"),
        ("acknowledged", "Просмотрен врачом"),
        ("escalated", "Передан резервному врачу"),
        ("resolved", "Закрыт"),
    ]
    OPEN = ("new", "delivered", "escalated")

    patient_id = models.BigIntegerField(db_index=True, help_text="ID пациента в основной модели проекта")
    patient_label = models.CharField(max_length=64, help_text="метка без ФИО, например «П-0042» — уходит в мессенджер")
    card_url = models.CharField(max_length=255, blank=True, help_text="ссылка на карточку пациента в кабинете врача")
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alerts")
    backup_doctor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    rule_id = models.CharField(max_length=64)
    severity = models.CharField(max_length=16, choices=SEVERITY)
    title = models.CharField(max_length=255)
    facts = models.JSONField(default=dict, help_text="снимок фактов, на которых сработало правило")
    patient_actions = models.JSONField(default=list)
    doctor_actions = models.JSONField(default=list)
    status = models.CharField(max_length=16, choices=STATUS, default="new", db_index=True)

    source_received_at = models.DateTimeField(help_text="когда сервер получил данные пациента")
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resolution = models.TextField(blank=True, help_text="что сделал врач")
    retries = models.PositiveSmallIntegerField(default=0)
    escalated_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="escalated_alerts")
    escalated_at = models.DateTimeField(null=True, blank=True)
    false_positive = models.BooleanField(default=False, help_text="врач отметил «ложное срабатывание»")
    exhausted_at = models.DateTimeField(null=True, blank=True, help_text="повторы исчерпаны, никто не отметил — записано в журнал")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["doctor", "status", "severity"])]
        constraints = [
            # не больше одного ОТКРЫТОГО сигнала по одному правилу на пациента (защита от гонки при одновременных событиях)
            models.UniqueConstraint(fields=["patient_id", "rule_id"], condition=models.Q(status__in=("new", "delivered", "escalated")),
                                    name="uniq_open_alert_per_rule"),
        ]
        verbose_name = "сигнал"
        verbose_name_plural = "сигналы"

    def __str__(self):
        return f"[{self.get_severity_display()}] {self.title} — {self.patient_label}"

    @property
    def delivery_latency_ms(self):
        if self.delivered_at:
            return round((self.delivered_at - self.source_received_at).total_seconds() * 1000, 1)
        return None

    def as_dict(self):
        return {
            "id": self.id, "patient_id": self.patient_id, "patient_label": self.patient_label, "card_url": self.card_url,
            "rule_id": self.rule_id, "severity": self.severity, "severity_label": self.get_severity_display(),
            "title": self.title, "status": self.status, "status_label": self.get_status_display(),
            "created_at": self.created_at.isoformat(), "delivery_latency_ms": self.delivery_latency_ms,
            "patient_actions": self.patient_actions, "doctor_actions": self.doctor_actions,
            "phrases": self.facts.get("suicide_keyword_matches", []) if self.rule_id == "suicide_risk_text" else [],
            "resolution": self.resolution, "false_positive": self.false_positive,
            "escalated_to_id": self.escalated_to_id,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_by_id": self.acknowledged_by_id,
        }


class AlertDelivery(models.Model):
    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=16)
    attempt = models.PositiveSmallIntegerField()
    recipient = models.CharField(max_length=128, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField()
    error = models.TextField(blank=True)

    class Meta:
        verbose_name = "попытка доставки"
        verbose_name_plural = "попытки доставки"


class AlertRecipient(models.Model):
    """Куда ещё, кроме кабинета, слать врачу срочные сигналы. Заполняется в админке."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alert_recipient")
    telegram_chat_id = models.CharField(max_length=64, blank=True)
    email = models.EmailField(blank=True, help_text="если пусто — берётся e-mail пользователя")

    class Meta:
        verbose_name = "контакты врача для сигналов"
        verbose_name_plural = "контакты врачей для сигналов"
