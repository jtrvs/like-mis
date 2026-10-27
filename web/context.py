"""Данные для боковой колонки кабинета пациента на широком экране: врач, поддержка, тёплая фраза дня."""
from datetime import date

PHRASES = [
    "Маленькие шаги тоже считаются.",
    "Не обязательно справляться со всем сразу.",
    "Отметить своё состояние — уже забота о себе.",
    "Тяжёлые дни проходят. Вы не один и не одна.",
    "Улучшение приходит постепенно — это нормально.",
    "Можно просто отдохнуть. Это тоже часть лечения.",
    "Спросить о помощи — это сила, а не слабость.",
]


def patient_side(request):
    p = getattr(request, "patient", None)
    if p is None or not request.path.startswith("/me/"):
        return {}
    link = p.care_links.filter(active=True, is_lead=True).select_related("doctor__user").first()
    return {"side_doctor": link.doctor.user.get_full_name() if link else "", "side_phrase": PHRASES[date.today().toordinal() % len(PHRASES)]}
