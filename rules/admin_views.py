import os
import yaml
from django.shortcuts import render, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.conf import settings
from django.core.cache import cache
from .models import RuleVersion
from clinical.engine import loader

@staff_member_required
def upload_rules(request):
    if request.method == 'POST':
        file_type = request.POST.get('file_type')
        content = request.POST.get('content')
        
        # Валидация YAML
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            messages.error(request, f"Ошибка в YAML: {e}")
            return render(request, 'admin/upload_rules.html', {
                'file_types': RuleVersion.FILE_TYPES,
                'initial_content': content,
                'default_file_type': file_type
            })
        
        # Деактивируем старые
        RuleVersion.objects.filter(file_type=file_type, is_active=True).update(is_active=False)
        
        # Считаем новую версию
        last_version = RuleVersion.objects.filter(file_type=file_type).order_by('-version').first()
        new_version_num = (last_version.version + 1) if last_version else 1
        
        # Создаем новую
        RuleVersion.objects.create(
            file_type=file_type,
            content=content,
            version=new_version_num,
            is_active=True,
            uploaded_by=request.user,
            comment=request.POST.get('comment', '')
        )
        
        # Сбрасываем кеши
        loader.reload()
        cache.delete('alert_rules_cache')

        messages.success(request, "Правила успешно загружены и активированы!")
        return redirect('admin:index')
        
    # === GET запрос: показываем форму с текущим содержимым ===
    default_file_type = request.GET.get('file_type', 'alerts')
    initial_content = ""
    
    # 1. Пытаемся взять активную версию из базы
    active = RuleVersion.objects.filter(file_type=default_file_type, is_active=True).first()
    if active:
        initial_content = active.content
    else:
        # 2. Если в базе нет — читаем файл из репозитория (fallback)
        file_paths = {
            'alerts': 'alerts/data/alert_rules.yaml',
            'routing': 'clinical/data/routing_rules.yaml',
            'aviandr': 'clinical/data/drugs/aviandr.yaml',
        }
        rel_path = file_paths.get(default_file_type)
        if rel_path:
            full_path = os.path.join(settings.BASE_DIR, rel_path)
            if os.path.exists(full_path):
                with open(full_path, 'r', encoding='utf-8') as f:
                    initial_content = f.read()

    return render(request, 'admin/upload_rules.html', {
        'file_types': RuleVersion.FILE_TYPES,
        'initial_content': initial_content,
        'default_file_type': default_file_type,
    })
