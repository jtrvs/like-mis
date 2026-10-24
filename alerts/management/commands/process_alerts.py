"""
Фоновый процесс сигналов. В docker-compose — сервис alerts-worker:
  python manage.py process_alerts --loop

Раз в минуту: повтор неподтверждённых сигналов, передача резервному врачу, запись «без ответа» в журнал.
Раз в час (если задан ALERTS["SCHEDULE_HOOK"]): плановая проверка пациентов, которые ничего не присылают
(например, перестали отмечать приём таблеток) — правила с events: [schedule].
"""
import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils.module_loading import import_string

from alerts import service

log = logging.getLogger("alerts")


def run_schedule_hook():
    path = getattr(settings, "ALERTS", {}).get("SCHEDULE_HOOK")
    if not path:
        return 0
    n = 0
    for kwargs in import_string(path)():  # функция Юли отдаёт аргументы для report_event по каждому пациенту
        n += len(service.report_event(trigger="schedule", **kwargs)["alerts"])
    return n


class Command(BaseCommand):
    help = "Повтор/эскалация неподтверждённых сигналов (--loop — работать постоянно)"

    def add_arguments(self, p):
        p.add_argument("--loop", action="store_true")
        p.add_argument("--interval", type=int, default=60)

    def handle(self, loop, interval, **kw):
        last_schedule = 0.0
        while True:
            try:
                if "alerts_alert" not in connection.introspection.table_names():
                    self.stdout.write("жду миграций базы (их выполняет сервис web)...")
                else:
                    stats = service.process_pending()
                    if time.time() - last_schedule >= 3600:
                        stats["schedule"] = run_schedule_hook()
                        last_schedule = time.time()
                    self.stdout.write(str(stats))
            except Exception:  # одна ошибка (например, база перезапускается) не должна останавливать повторы
                log.exception("process_alerts: ошибка цикла, повторю через %s с", interval)
                connection.close()
            if not loop:
                break
            time.sleep(interval)
