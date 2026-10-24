"""
Кто считается врачом. Пока у проекта нет своих ролей: участник группы «doctor» или у пользователя есть
профиль врача (user.doctor). Администратор (is_staff) без этого врачом НЕ считается.
Когда Юля сделает роли — достаточно поправить эту одну функцию.
"""


def is_doctor(user) -> bool:
    if not user.is_authenticated:
        return False
    return hasattr(user, "doctor") or user.groups.filter(name="doctor").exists()
