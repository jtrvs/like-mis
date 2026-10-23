"""Повтор и эскалация неподтверждённых сигналов. В docker-compose запускается как сервис alerts-worker:
  python manage.py process_alerts --loop
"""
import time

from django.core.management.base import BaseCommand

from alerts import service


class Command(BaseCommand):
    help = "Повтор/эскалация неподтверждённых сигналов (--loop — работать постоянно)"

    def add_arguments(self, p):
        p.add_argument("--loop", action="store_true")
        p.add_argument("--interval", type=int, default=60)

    def handle(self, loop, interval, **kw):
        while True:
            self.stdout.write(str(service.process_pending()))
            if not loop:
                break
            time.sleep(interval)
