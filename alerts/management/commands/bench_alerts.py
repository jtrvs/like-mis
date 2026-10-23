"""
Замер для слайда: «время доставки сигнала врачу с момента поступления данных».
  python manage.py bench_alerts --n 300
Создаёт тестовые сигналы — запускать на тестовой базе, не на боевой (или удалить их потом в админке).
"""
import statistics
import time

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from alerts import facts, service
from alerts.models import Alert


class Command(BaseCommand):
    def add_arguments(self, p):
        p.add_argument("--n", type=int, default=100)

    def handle(self, n, **kw):
        doc, _ = User.objects.get_or_create(username="bench_doctor")
        base = 9_000_000 + int(time.time()) % 100000 * 1000
        t0 = time.perf_counter()
        for i in range(n):
            service.report_event(patient_id=base + i, patient_label=f"B-{i}", doctor=doc, trigger="scale",
                                 facts=facts.from_scale("phq9", [1, 1, 1, 1, 1, 1, 1, 1, 2]))
        wall = time.perf_counter() - t0
        lat = sorted(a.delivery_latency_ms for a in Alert.objects.filter(doctor=doc, patient_id__gte=base, patient_id__lt=base + n))
        q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))]
        self.stdout.write(f"сигналов: {len(lat)} из {n}\n"
                          f"доставка, мс: медиана {statistics.median(lat):.1f}, p95 {q(0.95):.1f}, макс {lat[-1]:.1f}\n"
                          f"пропускная способность: {n / wall:.0f} событий/с (один процесс)")
