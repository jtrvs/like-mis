"""
Кто считается врачом. Пока у проекта нет своих ролей, подходят три варианта:
сотрудник (is_staff), участник группы «doctor» или у пользователя есть профиль врача (user.doctor).
Когда Юля сделает роли — достаточно поправить эту одну функцию.
"""


def is_doctor(user) -> bool:
    if not user.is_authenticated:
        return False
    return user.is_staff or hasattr(user, "doctor") or user.groups.filter(name="doctor").exists()
