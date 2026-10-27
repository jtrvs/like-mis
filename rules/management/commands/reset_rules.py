"""
Сделать активными правила из репозитория (снять активность со всех версий, загруженных через админку).
Запускать при выкатке новой версии правил:  python manage.py reset_rules
"""
from django.core.management.base import BaseCommand

from clinical.engine import loader
from rules.models import RuleVersion


class Command(BaseCommand):
    help = "Снять активность с версий правил из админки — работают файлы из репозитория"

    def handle(self, **kw):
        n = RuleVersion.objects.filter(is_active=True).update(is_active=False)
        loader.reload()
        self.stdout.write(self.style.SUCCESS(f"Деактивировано версий: {n}. Работают правила из репозитория."))
