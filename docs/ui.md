# Интерфейсы врача и пациента (ветка `stas/ui-prototype`)

Рабочий прототип экранов «Лямбды» — основа для Стаса: запускается, всё кликается, можно править как угодно.
Ветка сделана **поверх** `maxim/role4-alerts-extraction` (сигналы, разбор выписок, проверка Авиандра), поэтому включает и её.

## Запуск
```bash
git fetch origin
git checkout stas/ui-prototype
cp .env.example .env            # если ещё нет
docker compose up --build
docker compose exec web python manage.py seed_demo --reset      # демо-пациенты и врачи
```
Открыть http://localhost:8000/login/ — пароль у всех `Demo-2026!`:

| Логин | Кто | Что посмотреть |
|---|---|---|
| `sidorov` | терапевт | Морозов — срочные сигналы и обязательное направление к психиатру; Волков — Авиандр «жёлтый» (вне показаний + СИОЗС) |
| `petrova` | психиатр | Смирнова — Авиандр работает, графики улучшения; Кузнецова — Авиандр «красный» (печень + ИМАО); Новикова — алкоголь на Авиандре |
| `anna` | пациент (лучше открыть с телефона или в узком окне) | отметка дня, таблетки, опросники, динамика, «Мне плохо» |

Новый пациент: врач создаёт карточку → получает код → пациент регистрируется на `/join/`.

## Где что лежит
| Что | Файл |
|---|---|
| Стили (цвета, шрифт, сигналы «обязательно/желательно») | `web/static/web/app.css` — цвета заданы переменными в начале файла (`--brand`, `--red`, `--amber` …) |
| Каркас страниц врача (меню слева) | `web/templates/web/doctor/base.html` |
| Список пациентов | `web/templates/web/doctor/home.html` + `views_doctor.doctor_home` |
| Карточка пациента (главный экран) | `web/templates/web/doctor/patient.html` + `views_doctor.patient_card` |
| Карточка сигнала | `web/templates/web/doctor/_alert.html` |
| Загрузка выписки | `web/templates/web/doctor/import.html` |
| Экраны пациента (мобильные) | `web/templates/web/patient/*.html` + `views_patient.py` |
| Вход, регистрация по коду | `web/templates/web/login.html`, `join.html`, `views_common.py` |
| Права (врач видит только своих) | `web/access.py` |
| Подсчёты для экранов (риск, приверженность, расписание таблеток) | `web/services.py` |
| Связь с модулем сигналов | `web/alerts_bridge.py` — после сохранения данных пациента вызывается `report(...)` |
| Демо-данные | `web/management/commands/seed_demo.py` |
| Шрифт Golos Text, Chart.js, htmx | `web/static/web/fonts`, `web/static/web/vendor` — локально, без внешних CDN |

Шаблоны — обычный Django: `{{ переменная }}`, `{% if %}`, `{% for %}`. Графики — Chart.js в конце `patient.html` и `patient/progress.html`.

## Модели (`core/`) — черновик для Юли
Экранам нужны пациент, врач, связь врач-пациент, назначения, дневник, шкалы и т. д. Они лежат в `core/models.py` — это черновик модели данных из прототипа. Если Юля сделает свои модели, меняются запросы во `views_*.py`, `services.py` и `alerts_bridge.py`; шаблоны почти не меняются.

## Проверка, что ничего не сломалось
```bash
docker compose exec web python manage.py test              # 66 тестов
python e2e/scenarios.py --base http://localhost:8000       # 9 сценариев в браузере (нужен playwright)
python e2e/screens.py --base http://localhost:8000         # скриншоты экранов в docs/screens
```

## Что изменено в общих настройках в этой ветке
`like_mis/settings.py`: приложения `core` и `web`, русский язык, время МСК, адреса входа/выхода; `like_mis/urls.py`: подключены экраны.
