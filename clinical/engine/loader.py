"""Загрузка справочников из YAML или базы данных.
Правила и шкалы читаются из БД (если есть активная версия), иначе — из файлов.
Новые шкалы/препараты добавляются файлом или через админку, без правки кода.
"""
from functools import lru_cache
from pathlib import Path
import yaml

# Импортируем модель. Важно: импорт внутри функции или в начале,
# чтобы не было циклических зависимостей (rules не должен импортировать clinical).
from rules.models import RuleVersion

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

def _load(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

def _get_db_rule(file_type: str):
    """Пытается получить активную версию правила из базы."""
    try:
        active_version = RuleVersion.objects.filter(file_type=file_type, is_active=True).first()
        if active_version:
            return yaml.safe_load(active_version.content)
    except Exception:
        # Если БД недоступна (например, при миграциях), молча падаем в файл
        pass
    return None

@lru_cache
def drugs() -> dict:
    # Для препаратов пока оставляем файлы (или можно добавить тип 'aviandr' в БД)
    return {d["code"]: d for d in (_load(p) for p in sorted((DATA_DIR / "drugs").glob("*.yaml")))}

@lru_cache
def scales() -> dict:
    # Шкалы пока оставляем в файлах (по совету Claude, их менять на лету опасно)
    return {s["code"]: s for s in (_load(p) for p in sorted((DATA_DIR / "scales").glob("*.yaml")))}

@lru_cache
def drug_classes() -> dict:
    return {k.lower(): v for k, v in _load(DATA_DIR / "drug_classes.yaml").items()}

@lru_cache
def routing_rules() -> list:
    # Сначала пробуем БД
    db_data = _get_db_rule('routing')
    if db_data:
        return db_data.get("rules", [])
    # Если нет — файл
    return _load(DATA_DIR / "routing_rules.yaml")["rules"]

@lru_cache
def trade_names() -> dict:
    p = DATA_DIR.parent.parent / "extraction" / "data" / "drug_names.yaml"
    return {k.lower(): v for k, v in _load(p).items()} if p.exists() else {}

def reload():
    """Сброс кеша. Вызывать после загрузки новых правил через админку."""
    for fn in (drugs, scales, drug_classes, routing_rules, trade_names):
        fn.cache_clear()
