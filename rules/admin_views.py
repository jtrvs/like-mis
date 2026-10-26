import yaml
from django.shortcuts import render, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
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
            return render(request, 'admin/upload_rules.html', {'file_types': RuleVersion.FILE_TYPES})
        
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
        loader.reload()

        from alerts.service import load_rules as load_alert_rules
        load_alert_rules.cache_clear()

        messages.success(request, "Правила успешно загружены и активированы!")
        return redirect('admin:index')
        
    return render(request, 'admin/upload_rules.html', {'file_types': RuleVersion.FILE_TYPES})
