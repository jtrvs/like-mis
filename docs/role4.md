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
- `docker-compose.yml`: два новых сервиса — `alerts-worker` (повтор сигналов) и `backup` (копия базы каждые 15 минут). `db` и `web` не тронуты;
- `.gitignore`: папка `backups/`.

## Как подключить к моделям Юли — одна функция

После того как сохранились данные пациента (ответ на шкалу, отметка в дневнике, сообщение врачу, отметка о приёме таблетки):

```python
from alerts import facts, service

result = service.report_event(
    patient_id=patient.id,               # ID пациента в вашей модели
    patient_label=f"П-{patient.id:04d}", # метка БЕЗ ФИО — уйдёт в мессенджер
    doctor=lead_doctor_user,             # User ведущего врача
    backup_doctor=backup_user,           # User резервного врача (можно None)
    trigger="scale",                     # "scale" | "checkin" | "message" | "intake" | "schedule"
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

## Для интерфейса (Стас)
- `GET /alerts/` — готовая страница сигналов врача (можно взять как есть или как образец);
- `GET /api/alerts/?open=1` — сигналы текущего врача в JSON; `?patient_id=42` — по пациенту;
- `POST /api/alerts/<id>/ack/` с `{"resolution": "что сделано"}` — отметить просмотренным;
- `POST /api/extract` с `{"text": "..."}` — разбор выписки.

Кто считается врачом — функция `alerts/permissions.py: is_doctor` (сейчас: `is_staff`, группа `doctor` или `user.doctor`). Когда появятся роли, правится только она.

## Как посмотреть самому
```bash
docker compose up --build
docker compose exec web python manage.py alerts_demo
```
Открыть http://localhost:8000/admin/login/?next=/alerts/, войти `doctor_demo` / `Demo-2026!`.

## Команды
```bash
docker compose exec web python manage.py test                     # тесты (28)
docker compose exec web python manage.py bench_alerts --n 300     # замер скорости сигналов (на тестовой базе)
docker compose exec web python extraction/evaluate.py extraction/data/histories/test   # точность разбора выписок
docker compose exec backup sh /ops/restore.sh                     # восстановить последнюю копию базы
python scripts/loadtest.py --url http://localhost:8000/api/extract --cookie "sessionid=..."   # нагрузка
```

## Цифры (PostgreSQL 16, проверено на этой ветке)
- сигнал врачу: медиана **4,3 мс**, p95 **5,8 мс** (300 событий, потерь 0);
- разбор выписок: **F1 = 0,935** на 12 историях, которых система не видела при разработке;
- тесты: **28/28**.

## Замечания к основному каркасу (не трогал, на усмотрение Юли)
- `Dockerfile`: в последней строке `CMD [...]` не хватает закрывающей `]` — сейчас не мешает, потому что `docker-compose.yml` задаёт свою команду, но на продакшене с `CMD` упадёт.
- `TIME_ZONE = 'UTC'` — время в интерфейсе будет по Гринвичу; для врачей удобнее `Europe/Moscow`. Страница сигналов уже показывает МСК сама.
- Сервис `web` получает из окружения только `DJANGO_SECRET_KEY`; `DEBUG` и `ALLOWED_HOSTS` из `.env` в контейнер не попадают (берутся значения по умолчанию).
