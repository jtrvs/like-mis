"""
Ядро модели данных «Лямбда» (черновик для Юли).

Принципы:
- Пациент привязан к ведущему врачу через CareLink; данные пациента видит только врач,
  у которого есть активная связь (требование кейса «связь врач-пациент»).
- Система ничего не назначает сама: всё клинически значимое проходит через DoctorDecision.
- Любое чтение/изменение карточки пишется в AuditLog (152-ФЗ, метрика «защищённость»).
- Справочники (шкалы, препараты, правила алертов) лежат вне ядра: в приложениях
  clinical/ и alerts/ и в YAML-конфигах, поэтому расширяются без правки этих моделей.
"""
import uuid

from django.conf import settings
from django.db import models


class Role(models.TextChoices):
    DOCTOR = "doctor", "Врач"
    PATIENT = "patient", "Пациент"
    ADMIN = "admin", "Администратор"


class Profile(models.Model):
    """Расширение стандартного пользователя Django: роль и контакты."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=16, choices=Role.choices)
    phone = models.CharField(max_length=32, blank=True)
    telegram_chat_id = models.CharField(max_length=64, blank=True, help_text="для доставки алертов врачу")

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.get_role_display()})"


class DoctorProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="doctor")
    specialty = models.CharField(
        max_length=32,
        choices=[
            ("psychiatrist", "Психиатр"),
            ("psychotherapist", "Психотерапевт"),
            ("neurologist", "Невролог"),
            ("therapist", "Терапевт / ВОП"),
            ("other", "Другая"),
        ],
    )
    organization = models.CharField(max_length=255, blank=True)
    # куда эскалировать алерт, если врач не подтвердил его вовремя
    backup_doctor = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)

    @property
    def is_psychiatrist(self):
        return self.specialty in ("psychiatrist", "psychotherapist")


class Sex(models.TextChoices):
    M = "M", "Мужской"
    F = "F", "Женский"


class PatientProfile(models.Model):
    """
    Врач видит настоящее ФИО (запрос психиатра из кастдева).
    Внешний идентификатор (public_id) используется в URL, логах и при передаче в сторонние
    сервисы — так ФИО не утекает за пределы карточки.
    """

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="patient"
    )
    last_name = models.CharField(max_length=128)
    first_name = models.CharField(max_length=128)
    middle_name = models.CharField(max_length=128, blank=True)
    birth_date = models.DateField()
    sex = models.CharField(max_length=1, choices=Sex.choices)

    # клинически значимые факторы для проверки безопасности терапии
    is_pregnant = models.BooleanField(default=False)
    is_breastfeeding = models.BooleanField(default=False)
    egfr = models.FloatField(null=True, blank=True, help_text="СКФ, мл/мин/1,73 м²")
    child_pugh_class = models.CharField(max_length=1, blank=True, help_text="A/B/C, пусто — нет печёночной недостаточности")
    post_covid = models.BooleanField(default=False, help_text="тревога связана с перенесённым COVID-19 (показание Авиандра F41.9/F43.9)")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.last_name} {self.first_name} {self.middle_name}".strip()


class CareLink(models.Model):
    """Связь врач-пациент. Пациент сам вводит код приглашения от врача."""

    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name="care_links")
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="care_links")
    is_lead = models.BooleanField(default=True, help_text="ведущий лечащий врач")
    invite_code = models.CharField(max_length=16, unique=True)
    invite_expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["doctor", "patient"], name="uniq_doctor_patient"),
        ]


class Consent(models.Model):
    """Согласия пациента (152-ФЗ, ст. 10 — данные о здоровье) и на звонки доверенным лицам."""

    KINDS = [
        ("pd_processing", "Обработка персональных данных о здоровье"),
        ("remote_monitoring", "Дистанционное наблюдение"),
        ("emergency_contacts", "Связь врача с доверенными лицами в экстренной ситуации"),
        ("share_with_psychologist", "Доступ психолога к данным"),
    ]
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="consents")
    kind = models.CharField(max_length=32, choices=KINDS)
    granted = models.BooleanField()
    granted_at = models.DateTimeField(auto_now_add=True)
    document_version = models.CharField(max_length=32, default="v1")


class TrustedContact(models.Model):
    """Доверенные лица (пункты 7-9 кастдева): кому может позвонить пациент и/или врач."""

    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="trusted_contacts")
    name = models.CharField(max_length=128)
    relation = models.CharField(max_length=64, blank=True)
    phone = models.CharField(max_length=32)
    patient_may_call = models.BooleanField(default=True)
    doctor_may_call = models.BooleanField(default=False, help_text="галочку ставит сам пациент")


class Diagnosis(models.Model):
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="diagnoses")
    icd10 = models.CharField(max_length=8, help_text="например F41.1")
    title = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=True)
    established_by = models.ForeignKey(DoctorProfile, null=True, on_delete=models.SET_NULL)
    established_on_visit = models.BooleanField(
        default=True, help_text="поставлен на очном приёме (условие для телемедицины, 323-ФЗ ст. 36.2)"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class Condition(models.Model):
    """Сопутствующие состояния, важные для противопоказаний: эпилепсия, ССЗ, зависимость и т.д."""

    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="conditions")
    code = models.CharField(max_length=64, help_text="ключ из словаря clinical/data/conditions.yaml")
    note = models.CharField(max_length=255, blank=True)


class Prescription(models.Model):
    """Назначение. Создаётся только врачом (status=active после DoctorDecision)."""

    STATUS = [("proposed", "Предложено системой"), ("active", "Действует"), ("stopped", "Отменено")]
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="prescriptions")
    drug_code = models.CharField(max_length=64, help_text="ключ из clinical/data/drugs/*.yaml или МНН")
    drug_name = models.CharField(max_length=128)
    dose_mg = models.FloatField()
    times_per_day = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS, default="active")
    prescribed_by = models.ForeignKey(DoctorProfile, null=True, on_delete=models.SET_NULL)
    off_label = models.BooleanField(default=False, help_text="решение вне инструкции — только врач, с комментарием")
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)


class MedicationIntake(models.Model):
    """Дневник приёма (кастдев п.3): из него считается приверженность."""

    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name="intakes")
    scheduled_at = models.DateTimeField()
    taken = models.BooleanField(null=True, help_text="None — пациент не отметил")
    actual_dose_mg = models.FloatField(null=True, blank=True)
    marked_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["prescription", "scheduled_at"], name="uniq_intake_slot")]


class ScaleResponse(models.Model):
    """Заполненная шкала. Сама шкала описана в clinical/data/scales/*.yaml."""

    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="scale_responses")
    scale_code = models.CharField(max_length=32, help_text="phq9, gad7, ...")
    answers = models.JSONField(help_text="список баллов по пунктам")
    total = models.IntegerField()
    severity = models.CharField(max_length=32)
    filled_by = models.CharField(max_length=16, choices=[("patient", "Пациент"), ("doctor", "Врач")])
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class DailyCheckin(models.Model):
    """
    Короткий ежедневный самоотчёт пациента. Обе шкалы направлены одинаково — 10 значит «хорошо»:
    пациент отмечает «Настроение» и «Спокойствие»; в базе спокойствие хранится как тревога (anxiety = 10 − спокойствие),
    чтобы правила и графики врача оставались в клиническом смысле («тревога выросла»).
    kind: full — обычная запись; one — ответ на один вопрос после паузы; worse — кнопка «Мне сегодня хуже».
    """

    KINDS = [("full", "Запись дня"), ("one", "Один вопрос"), ("worse", "«Мне сегодня хуже»")]
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="checkins")
    kind = models.CharField(max_length=8, choices=KINDS, default="full")
    mood = models.PositiveSmallIntegerField(null=True, blank=True)
    anxiety = models.PositiveSmallIntegerField(null=True, blank=True)
    sleep_hours = models.FloatField(null=True, blank=True)
    left_home = models.BooleanField(null=True)
    ate_normally = models.BooleanField(null=True)
    # алкоголь: пациент указывает напиток, объём и крепость, стандартные порции (10 г спирта) система считает сама
    alcohol_units = models.FloatField(default=0)
    alcohol_drink = models.CharField(max_length=64, blank=True)
    alcohol_ml = models.PositiveIntegerField(null=True, blank=True)
    alcohol_abv = models.FloatField(null=True, blank=True, help_text="крепость, % об.")
    other_substances = models.BooleanField(null=True, help_text="принимал что-то, что не назначал врач")
    other_substances_text = models.CharField(max_length=255, blank=True)
    joy = models.CharField(max_length=500, blank=True, help_text="«Что сегодня могло бы вас порадовать?»")
    private_text = models.TextField(blank=True, help_text="личная заметка — видна только пациенту")

    @property
    def calm(self):
        return None if self.anxiety is None else 10 - self.anxiety
    side_effects = models.JSONField(default=list, blank=True)
    # 0 — нет, 1 — слегка, 2 — сильно, 3 — мешают обычной жизни (правило 2.9)
    side_effect_level = models.PositiveSmallIntegerField(default=0)
    serotonin_signs = models.JSONField(default=list, blank=True, help_text="признаки серотонинового синдрома (правило 1.6)")
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class PatientReport(models.Model):
    """Пациент сообщает врачу о приёме лекарства или о беременности (правила 1.4, 1.5, 4.4–4.6)."""

    KINDS = [
        ("extra_dose", "Принял(а) больше назначенного"),
        ("stopped", "Перестал(а) принимать"),
        ("reduced", "Сам(а) уменьшил(а) дозу"),
        ("pregnancy", "Беременность"),
        ("breastfeeding", "Кормление грудью"),
    ]
    REASONS = [
        ("side_effects", "Побочные эффекты"), ("better", "Стало лучше"), ("expensive", "Дорого"),
        ("forget", "Забываю"), ("other", "Другое"),
    ]
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="reports")
    kind = models.CharField(max_length=16, choices=KINDS)
    extra_tablets = models.PositiveSmallIntegerField(null=True, blank=True, help_text="сколько таблеток сверх назначенного")
    feels_bad = models.BooleanField(null=True, blank=True)
    reason = models.CharField(max_length=16, choices=REASONS, blank=True)
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class Message(models.Model):
    """Канал коммуникации врач-пациент."""

    care_link = models.ForeignKey(CareLink, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)


class Referral(models.Model):
    """Направление к психиатру (задача 8). Система предлагает, врач подтверждает."""

    URGENCY = [("emergency", "Обязательно, экстренно"), ("mandatory", "Обязательно"), ("desirable", "Желательно")]
    STATUS = [("proposed", "Предложено"), ("confirmed", "Подтверждено врачом"), ("rejected", "Отклонено врачом")]
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="referrals")
    urgency = models.CharField(max_length=16, choices=URGENCY)
    reasons = models.JSONField(help_text="список причин с источниками")
    status = models.CharField(max_length=16, choices=STATUS, default="proposed")
    created_at = models.DateTimeField(auto_now_add=True)
    # обратная связь направившему врачу: психиатр принял пациента и оставил рекомендации
    accepted_by = models.ForeignKey(DoctorProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="accepted_referrals")
    accepted_at = models.DateTimeField(null=True, blank=True)
    recommendations = models.TextField(blank=True)


class CareTeamMember(models.Model):
    """Другие врачи пациента (психолог, невролог, гинеколог, эндокринолог) — контакты для связи с согласия пациента."""

    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="care_team")
    name = models.CharField(max_length=128)
    specialty = models.CharField(max_length=64)
    contact = models.CharField(max_length=128, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ServiceRequest(models.Model):
    """Заявка пациента на услугу клиники (доп. приём, психолог, КПТ) — показывается только по клиническому поводу."""

    STATUS = [("new", "Новая"), ("done", "Обработана")]
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="service_requests")
    service = models.CharField(max_length=32)
    title = models.CharField(max_length=128)
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=8, choices=STATUS, default="new")
    created_at = models.DateTimeField(auto_now_add=True)


class DoctorDecision(models.Model):
    """
    Единая точка фиксации клинических действий: назначение, изменение, отмена, направление,
    реакция на алерт. Требование кейса: «клинические действия — только после подтверждения врачом».
    """

    KINDS = [
        ("prescribe", "Назначение"),
        ("change_dose", "Изменение дозы"),
        ("stop", "Отмена"),
        ("refer", "Направление"),
        ("alert_response", "Реакция на алерт"),
        ("accept_recommendation", "Принята рекомендация системы"),
        ("reject_recommendation", "Отклонена рекомендация системы"),
    ]
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.PROTECT)
    patient = models.ForeignKey(PatientProfile, on_delete=models.CASCADE, related_name="decisions")
    kind = models.CharField(max_length=32, choices=KINDS)
    payload = models.JSONField(default=dict)
    rationale = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64)
    patient = models.ForeignKey(PatientProfile, null=True, blank=True, on_delete=models.SET_NULL)
    details = models.JSONField(default=dict)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
