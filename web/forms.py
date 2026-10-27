from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password

from core.models import CareLink, Condition, PatientProfile, PatientReport, TrustedContact

# Признаки серотонинового синдрома (таблица правил, 1.6) — спрашиваем, если Авиандр сочетается
# с антидепрессантом, трамадолом и др. (правило 2.4)
SEROTONIN_SIGNS = ["жар", "дрожь", "подёргивания мышц", "сильное возбуждение", "сильный пот", "диарея"]

CONDITION_CHOICES = [
    ("epilepsy", "Эпилепсия"),
    ("organic_brain_disease", "Органическое поражение головного мозга"),
    ("cardiovascular_disease", "Сердечно-сосудистое заболевание"),
    ("diabetes", "Сахарный диабет"),
    ("endocrine_disease", "Другое эндокринное заболевание"),
    ("substance_dependence", "Зависимость от алкоголя / ПАВ"),
    ("lactose_intolerance", "Непереносимость лактозы"),
    ("allergy_maritupirdine", "Аллергия на маритупирдин (Авиандр)"),
]


class NewPatientForm(forms.ModelForm):
    dx_title = forms.CharField(label="Формулировка диагноза", max_length=255, required=False)
    conditions = forms.MultipleChoiceField(label="Сопутствующие состояния", choices=CONDITION_CHOICES, required=False, widget=forms.CheckboxSelectMultiple)
    child_pugh_class = forms.ChoiceField(label="Печёночная недостаточность (класс по Чайлд-Пью)", choices=[("", "нет"), ("A", "A"), ("B", "B"), ("C", "C")], required=False)
    egfr = forms.FloatField(label="СКФ, мл/мин/1,73 м²", min_value=1, max_value=200, required=False)
    icd10 = forms.RegexField(label="Диагноз (код МКБ-10)", regex=r"^[A-Za-zА-Яа-я]\d{2}(\.\d{1,2})?$", max_length=8, required=False,
                             error_messages={"invalid": "Код в формате F41.1"})

    class Meta:
        model = PatientProfile
        fields = ["last_name", "first_name", "middle_name", "birth_date", "sex", "is_pregnant", "is_breastfeeding", "post_covid", "egfr", "child_pugh_class"]
        labels = {"last_name": "Фамилия", "first_name": "Имя", "middle_name": "Отчество", "birth_date": "Дата рождения", "sex": "Пол",
                  "is_pregnant": "Беременность", "is_breastfeeding": "Грудное вскармливание",
                  "post_covid": "Тревога связана с перенесённым COVID-19", "egfr": "СКФ, мл/мин/1,73 м²",
                  "child_pugh_class": "Печёночная недостаточность (класс по Чайлд-Пью)"}
        widgets = {"birth_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "child_pugh_class": forms.Select(choices=[("", "нет"), ("A", "A"), ("B", "B"), ("C", "C")])}


class JoinForm(forms.Form):
    code = forms.CharField(label="Код от врача", max_length=16)
    username = forms.CharField(label="Придумайте логин", max_length=64)
    password = forms.CharField(label="Пароль", widget=forms.PasswordInput(attrs={"data-show-password": "1"}), help_text="Не короче 8 символов")
    consent_pd = forms.BooleanField(label="Согласен(на) на обработку моих персональных данных, включая сведения о здоровье, лечащим врачом (152-ФЗ, ст. 10). Данные хранятся в России и не передаются работодателю, родственникам или другим людям")
    consent_remote = forms.BooleanField(label="Согласен(на) на дистанционное наблюдение: мои ответы и дневник попадают в карточку у моего врача")

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        from django.db.models import Q
        from django.utils import timezone
        link = (CareLink.objects.filter(invite_code=code, active=True, patient__user__isnull=True)
                .filter(Q(invite_expires_at__isnull=True) | Q(invite_expires_at__gt=timezone.now()))
                .select_related("patient").first())
        if not link:
            raise forms.ValidationError("Код не найден или уже использован. Уточните его у врача.")
        self.link = link
        return code

    def clean_username(self):
        u = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=u).exists():
            raise forms.ValidationError("Такой логин уже занят.")
        return u

    def clean_password(self):
        p = self.cleaned_data["password"]
        validate_password(p)
        return p


# Напитки: крепкость по умолчанию, пациент может поправить. Стандартная порция — 10 г спирта (ВОЗ).
DRINKS = [("", "Не пил(а)"), ("beer", "Пиво"), ("wine", "Вино"), ("strong", "Крепкий напиток (водка, коньяк, виски)"),
          ("cocktail", "Коктейль, сидр, слабоалкогольное"), ("other", "Другое")]
DEFAULT_ABV = {"beer": 5, "wine": 12, "strong": 40, "cocktail": 7, "other": 12}


def standard_units(ml, abv):
    """Стандартные порции: мл × крепость × 0,789 г/мл / 10 г."""
    if not ml or not abv:
        return 0
    return round(ml * abv / 100 * 0.789 / 10, 1)


class CheckinForm(forms.Form):
    # Обе шкалы направлены одинаково: 10 — хорошо («настроение отличное», «спокойно»)
    mood = forms.IntegerField(label="Настроение", min_value=0, max_value=10)
    calm = forms.IntegerField(label="Спокойствие", min_value=0, max_value=10)
    sleep_hours = forms.FloatField(label="Сон", min_value=0, max_value=24, required=False)
    left_home = forms.TypedChoiceField(label="Выходили из дома", choices=[("1", "Да"), ("0", "Нет")], coerce=lambda v: v == "1", required=False, empty_value=None)
    # «Нет» = 0–1 приём пищи за день (правило 1.9 «почти не ест»)
    ate_normally = forms.TypedChoiceField(label="Питание", choices=[("1", "Да"), ("0", "Нет")], coerce=lambda v: v == "1", required=False, empty_value=None)
    alcohol_drink = forms.ChoiceField(label="Алкоголь", choices=DRINKS, required=False)
    alcohol_ml = forms.IntegerField(label="Сколько, мл", min_value=1, max_value=5000, required=False)
    alcohol_abv = forms.FloatField(label="Крепость, %", min_value=0.5, max_value=96, required=False)
    other_substances = forms.TypedChoiceField(label="Что-то без назначения врача", choices=[("1", "Да"), ("0", "Нет")],
                                              coerce=lambda v: v == "1", required=False, empty_value=None)
    other_substances_text = forms.CharField(label="Что именно", required=False, max_length=255)
    side_effects = forms.MultipleChoiceField(label="Самочувствие", required=False, choices=[(x, x) for x in [
        "сонливость", "головокружение", "головная боль", "тошнота", "бессонница", "усиление тревоги", "слабость", "онемение или покалывание"]])
    side_effect_level = forms.TypedChoiceField(label="Насколько это мешает", required=False, coerce=int, empty_value=1,
                                               choices=[("1", "Слегка"), ("2", "Сильно"), ("3", "Мешает обычной жизни")])
    serotonin_signs = forms.MultipleChoiceField(label="Есть ли сейчас", required=False, choices=[(x, x) for x in SEROTONIN_SIGNS])
    joy = forms.CharField(label="Что сегодня могло бы вас порадовать?", required=False, max_length=500)
    text = forms.CharField(label="Своими словами", required=False, max_length=3000)
    private_text = forms.CharField(label="Личная заметка", required=False, max_length=3000)

    def clean(self):
        d = super().clean()
        drink = d.get("alcohol_drink")
        if drink:
            if not d.get("alcohol_ml"):
                self.add_error("alcohol_ml", "Укажите примерный объём в мл.")
            d["alcohol_abv"] = d.get("alcohol_abv") or DEFAULT_ABV.get(drink, 12)
        d["alcohol_units"] = standard_units(d.get("alcohol_ml"), d.get("alcohol_abv")) if drink else 0
        return d


class PatientReportForm(forms.ModelForm):
    kind = forms.ChoiceField(label="Что произошло", choices=PatientReport.KINDS, widget=forms.RadioSelect)
    extra_tablets = forms.IntegerField(label="Сколько таблеток сверх назначенного", min_value=1, max_value=200, required=False)
    feels_bad = forms.TypedChoiceField(label="Вы плохо себя чувствуете?", choices=[("1", "Да"), ("0", "Нет")],
                                       coerce=lambda v: v == "1", required=False, empty_value=None, widget=forms.RadioSelect)
    reason = forms.ChoiceField(label="Почему перестали", choices=[("", "—")] + PatientReport.REASONS, required=False)
    text = forms.CharField(label="Комментарий (необязательно)", required=False, max_length=1000, widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = PatientReport
        fields = ["kind", "extra_tablets", "feels_bad", "reason", "text"]

    def clean(self):
        d = super().clean()
        if d.get("kind") == "extra_dose" and not d.get("extra_tablets"):
            self.add_error("extra_tablets", "Укажите, сколько таблеток вы приняли сверх назначенного.")
        return d


class TrustedContactForm(forms.ModelForm):
    class Meta:
        model = TrustedContact
        fields = ["name", "relation", "phone", "patient_may_call", "doctor_may_call"]
        labels = {"name": "Имя", "relation": "Кем приходится", "phone": "Телефон",
                  "patient_may_call": "Показывать мне в разделе «Помощь»",
                  "doctor_may_call": "Врач может позвонить этому человеку, если мне срочно нужна помощь"}


class PrescribeForm(forms.Form):
    rationale = forms.CharField(label="Комментарий врача", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    override = forms.BooleanField(label="Принимаю решение вне инструкции под свою ответственность", required=False)
    start_date = forms.DateField(label="Начало приёма", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))


class ImportForm(forms.Form):
    text = forms.CharField(label="Текст выписки или истории болезни", widget=forms.Textarea(attrs={"rows": 12}))


def add_conditions(patient, codes):
    for c in codes:
        Condition.objects.get_or_create(patient=patient, code=c)
