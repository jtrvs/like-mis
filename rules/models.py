from django.db import models
from django.conf import settings

class RuleVersion(models.Model):
    FILE_TYPES = [
        ('alerts', 'Правила алертов (alert_rules.yaml)'),
        ('routing', 'Маршрутизация (routing_rules.yaml)'),
        ('aviandr', 'Проверка Авиандра (aviandr.yaml)'),
    ]
    file_type = models.CharField(max_length=20, choices=FILE_TYPES, verbose_name="Тип правил")
    content = models.TextField(verbose_name="Содержимое (YAML)")
    version = models.PositiveIntegerField(default=1, verbose_name="Версия")
    is_active = models.BooleanField(default=False, verbose_name="Активна")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="Кто загрузил")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата загрузки")
    comment = models.CharField(max_length=255, blank=True, verbose_name="Комментарий")

    def save(self, *args, **kwargs):
        # Если эту версию делают активной, деактивируем все остальные версии того же типа
        if self.is_active:
            RuleVersion.objects.filter(
                file_type=self.file_type, 
                is_active=True
            ).exclude(pk=self.pk).update(is_active=False)
        
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "Версия правил"
        verbose_name_plural = "Версии правил"
        ordering = ['-created_at']
