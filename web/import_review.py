"""Проверка дат и идентичности препаратов при переносе выписки."""
from django import forms
from django.utils import timezone

from extraction.engine.rules_extractor import inn_for


def medication_name(value):
    name = str(value).strip().lower().replace('ё', 'е')
    return 'маритупирдин' if name == 'aviandr' else inn_for(name)


def prescription_name(prescription):
    if prescription.drug_code == 'aviandr':
        return 'маритупирдин'
    return medication_name(prescription.drug_name)


def medication_code(inn):
    return 'aviandr' if medication_name(inn) == 'маритупирдин' else medication_name(inn)


class ImportMedicationDates(forms.Form):
    start_date = forms.DateField(label='Начало приёма по выписке', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    end_date = forms.DateField(label='Дата отмены по выписке', required=False, widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))

    def __init__(self, *args, stopped=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopped = stopped
        self.use_required_attribute = False  # Обязательность в браузере зависит от отметки препарата.
        if stopped:
            self.fields['end_date'].required = True
        else:
            del self.fields['end_date']

    def clean(self):
        data = super().clean()
        start, end = data.get('start_date'), data.get('end_date')
        today = timezone.localdate()
        if start and start > today:
            self.add_error('start_date', 'Для переноса текущего или ранее отменённого приёма дата не может быть в будущем.')
        if end and end > today:
            self.add_error('end_date', 'Дата отмены не может быть в будущем.')
        if start and end and end < start:
            self.add_error('end_date', 'Дата отмены не может быть раньше начала приёма.')
        return data


def review_rows(payload, post=None):
    current, stopped = [], []
    for i, medication in enumerate(payload.get('medications', [])):
        enabled = bool(medication.get('dose_mg') and medication.get('times_per_day'))
        selected = str(i) in post.getlist('med') if post is not None else enabled
        current.append({'medication': medication, 'index': str(i), 'enabled': enabled,
                        'selected': selected,
                        'form': ImportMedicationDates(post if selected and enabled else None, prefix=f'current-{i}')})
    for i, name in enumerate(payload.get('stopped_medications', [])):
        selected = str(i) in post.getlist('stopped') if post is not None else True
        stopped.append({'name': name, 'index': str(i), 'selected': selected,
                        'form': ImportMedicationDates(post if selected else None, prefix=f'stopped-{i}', stopped=True)})
    return current, stopped
