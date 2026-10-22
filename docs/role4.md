# Роль 4 (Максим): сигналы, разбор выписок, движок правил, бэкапы, замеры

Ветка `maxim/role4-alerts-extraction`. Три новых приложения Django, которые **не зависят от моделей пациента и врача** — подключаются к ним через вызов одной функции.

| Приложение | Что делает |
|---|---|
| `clinical/` | Движок правил без базы данных: подсчёт PHQ-9/GAD-7, проверка Авиандра по инструкции (светофор), маршрутизация к психиатру «обязательно/желательно». Справочники — YAML-файлы в `clinical/data/` |
| `alerts/` | Сигналы о критических состояниях: 9 правил в `alerts/data/alert_rules.yaml`, доставка в кабинет / мессенджер / почту, отметка врача, повтор через 15 мин, передача резервному врачу |
| `extraction/` | `POST /api/extract`: из текста выписки любого формата — диагнозы, препараты с дозами, шкалы, факторы риска + сразу проверка Авиандра |

Правила лежат в файлах, а не в базе (как и предлагала Юля): поменять порог = поправить строку в YAML, без миграций и админок.

## Что изменено в файлах Юли (минимально)
- `like_mis/settings.py`: три приложения в `INSTALLED_APPS` и блок `ALERTS` в конце;
- `like_mis/urls.py`: подключены `alerts.urls` и `extraction.urls`;
- `requirements.txt`: `PyYAML`;
- `docker-compose.yml`: два новых сервиса — `alerts-worker` (повтор сигналов, читает `.env`) и `backup` (копия базы каждые 15 минут), оба с `restart: unless-stopped`. `db` и `web` не тронуты;
- `.gitignore`: папка `backups/` (в копиях базы — персональные данные); новый `.dockerignore` — чтобы `.env`, `.git` и бэкапы не попадали в образ.

## Как подключить к моделям Юли — одна функция

После того как сохранились данные пациента (ответ на шкалу, отметка в дневнике, сообщение врачу, отметка о приёме таблетки):

```python
from alerts import facts, service

result = service.report_event(
    patient_id=patient.id,               # ID пациента в вашей модели
    patient_label=f"П-{patient.id:04d}", # метка БЕЗ ФИО (похожее на ФИО — ошибка ValueError)
    doctor=lead_doctor_user,             # User ведущего врача
    backup_doctor=backup_user,           # User резервного врача (можно None)
    trigger="scale",                     # "scale" | "checkin" | "message" | "intake" | "schedule" (иное — ValueError)
    facts=facts.from_scale("phq9", answers, previous_total=prev),   # см. ниже
    text="",                             # новый текст пациента, если есть
    card_url=f"/doctor/patient/{patient.id}/",
)
if result["show_crisis_screen"]:
    return redirect("экран помощи с телефонами доверия")   # пациенту — сразу
# result["doctor_actions"] содержит "propose_mandatory_referral" → создать ПРЕДЛОЖЕНИЕ направления (врач подтверждает)
```

Помощники для `facts` (принимают обычные списки, а не модели):

| Событие | Как собрать факты |
|---|---|
| Шкала | `facts.from_scale("phq9" или "gad7", answers, previous_total)` |
| Ежедневная отметка | `facts.from_checkins([последние отметки, новые первыми], on_aviandr=True/False)` — каждая отметка: `mood, anxiety, sleep_hours, left_home, ate_normally, alcohol_units, side_effects` |
| Сообщение врачу | `facts=None, text=сообщение` |
| Приём таблетки | `facts.from_intakes([{"scheduled_at": ..., "taken": True/False/None}, ...])` |

Экран помощи можно показать и без сигнала: `facts.needs_crisis_screen(text, phq9_answers)`.

**Пациенты, которые ничего не присылают** (перестали отмечать таблетки — правило `diary_abandoned`). Сигнал на событие не сработает, раз событий нет. Для этого фоновый процесс раз в час вызывает функцию из настроек:

```python
# like_mis/settings.py
ALERTS["SCHEDULE_HOOK"] = "patients.alerts_hook.silent_patients"

# patients/alerts_hook.py — отдаёт аргументы report_event по каждому пациенту на терапии
def silent_patients():
    for p in Patient.objects.filter(on_therapy=True):
        yield dict(patient_id=p.id, patient_label=f"П-{p.id:04d}", doctor=p.doctor, backup_doctor=p.backup_doctor,
                   facts=facts.from_intakes([...последние приёмы, новые первыми...]))
```

Проверка Авиандра для карточки:

```python
from clinical.engine import safety
findings = safety.check("aviandr", {
    "age": 34, "pregnant": False, "breastfeeding": False, "egfr": 95, "child_pugh": "",   # "", "A", "B", "C"
    "conditions": ["epilepsy"], "icd10": ["F41.1"], "current_drugs": ["сертралин"],
    "stopped_drugs": [{"name": "моклобемид", "days_ago": 5}], "post_covid": False,
})
safety.verdict(findings)   # "green" | "yellow" | "red"
```

Важно: «зелёный» выдаётся только при полных данных. Если беременность, ГВ, возраст или СКФ неизвестны — передавайте `None`, будет «жёлтый» с пояснением, что не проверено. Печёночная/почечная недостаточность без класса и цифры — `conditions: ["hepatic_failure"]` / `["renal_failure"]` → «красный». Названия препаратов можно передавать как угодно («Аурорикс 300 мг», «Золофт (сертралин)») — система приведёт их к МНН.

## Для интерфейса (Стас)
- `GET /alerts/` — готовая страница сигналов врача (можно взять как есть или как образец);
- `GET /api/alerts/?open=1` — сигналы текущего врача в JSON; `?patient_id=42` — по пациенту;
- `POST /api/alerts/<id>/ack/` с `{"resolution": "что сделано"}` — отметить просмотренным. Из JavaScript нужен заголовок `X-CSRFToken` (значение из cookie `csrftoken`), иначе Django ответит 403 — это защита, а не ошибка. Повторная отметка не перезаписывает первую (кто и когда отреагировал);
- `POST /api/extract` с `{"text": "..."}` — разбор выписки.

Кто считается врачом — функция `alerts/permissions.py: is_doctor` (сейчас: группа `doctor` или `user.doctor`; администратор без роли врача доступа не получает). Когда появятся роли, правится только она.

## Как работают повторы
Срочный/важный сигнал без отметки 15 минут → повтор ведущему врачу (до 2 раз) → передача резервному врачу и ему тоже до 2 повторов → если не ответил никто (или резервного нет) — запись уровня CRITICAL в журнал `docker compose logs alerts-worker` («СИГНАЛ БЕЗ ОТВЕТА»). Отметка врача в любой момент останавливает повторы.

В мессенджер/почту уходит только «Лямбда: новый сигнал «Срочно», пациент П-0042» и ссылка — без сути сигнала (это сведения о здоровье, а мессенджер — сторонний сервис).

## Как посмотреть самому
```bash
docker compose up --build
docker compose exec web python manage.py alerts_demo
```
Открыть http://localhost:8000/admin/login/?next=/alerts/, войти `doctor_demo` / `Demo-2026!`.

## Команды
```bash
docker compose exec web python manage.py test                     # тесты (45)
docker compose exec web python manage.py bench_alerts --n 300     # замер скорости сигналов (на тестовой базе)
docker compose exec web python extraction/evaluate.py extraction/data/histories/test   # точность разбора выписок
docker compose exec backup sh /ops/restore.sh                     # восстановить последнюю копию базы
python scripts/loadtest.py --url http://localhost:8000/api/extract --cookie "sessionid=..."   # нагрузка
```

## Цифры (PostgreSQL 16, проверено на этой ветке)
- сигнал врачу: медиана **4,3 мс**, p95 **5,8 мс** (300 событий, потерь 0);
- 8 одновременных одинаковых событий → ровно 1 сигнал (защита уникальным индексом в базе);
- разбор выписок: **F1 = 0,935** на 12 историях, которых система не видела при разработке;
- тесты: **45/45** на PostgreSQL 16 и Python 3.12 (как в Docker-образе); независимое ревью — 12 находок исправлены, на каждую есть тест.

## Замечания к основному каркасу (не трогал, на усмотрение Юли)
- `Dockerfile`: в последней строке `CMD [...]` не хватает закрывающей `]` — сейчас не мешает, потому что `docker-compose.yml` задаёт свою команду, но на продакшене с `CMD` упадёт.
- `TIME_ZONE = 'UTC'` — время в интерфейсе будет по Гринвичу; для врачей удобнее `Europe/Moscow`. Страница сигналов уже показывает МСК сама.
- Сервис `web` получает из окружения только `DJANGO_SECRET_KEY`; `DEBUG`, `ALLOWED_HOSTS`, `PUBLIC_BASE_URL` из `.env` в контейнер не попадают. Предлагаю добавить ему `env_file: .env`, как у `alerts-worker`.
- Для продакшена по https: `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT` (`manage.py check --deploy` про них предупреждает).
- Если будет своя модель пользователя (кастомный User) — её нужно сделать до первого `migrate`: таблица сигналов ссылается на пользователя.
