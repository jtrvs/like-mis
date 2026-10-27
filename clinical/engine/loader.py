"""
Загрузка справочников из YAML (репозиторий) или из активной версии в базе (админка «Версии правил»).

- Шкалы, классы препаратов и торговые названия — только файлы: менять подсчёт шкал «на лету» нельзя,
  иначе старые результаты пациентов перестанут сравниваться.
- Правила маршрутизации и карточка Авиандра — файл по умолчанию; если в админке активирована версия,
  берётся она. Правила сигналов читает alerts/service.py тем же способом.
- Каждый процесс (воркеры gunicorn, alerts-worker) сам замечает новую активную версию: перед чтением
  сверяется «отпечаток» активной версии в базе (один лёгкий запрос), кеш сбрасывается автоматически.
"""
from functools import lru_cache
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REPO_FILES = {
    "alerts": DATA_DIR.parent.parent / "alerts" / "data" / "alert_rules.yaml",
    "routing": DATA_DIR / "routing_rules.yaml",
    "aviandr": DATA_DIR / "drugs" / "aviandr.yaml",
}


def _load(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def active_version(file_type: str):
    """Активная версия правил из базы или None (нет версии / база недоступна, например при миграциях)."""
    try:
        from rules.models import RuleVersion
        return RuleVersion.objects.filter(file_type=file_type, is_active=True).only("id", "version", "content").first()
    except Exception:
        return None


_memo: dict = {}
CHECK_EVERY_S = 2.0  # как часто процесс сверяется с базой: новая версия правил подхватывается за ~2 секунды


def versioned(file_type: str):
    """Содержимое правил: активная версия из базы, иначе файл из репозитория. Кеш — по отпечатку версии."""
    import time
    hit = _memo.get(file_type)
    if hit and time.monotonic() - hit[2] < CHECK_EVERY_S:
        return hit[1]
    v = active_version(file_type)
    key = (v.id, v.version) if v else None
    if hit and hit[0] == key:
        _memo[file_type] = (key, hit[1], time.monotonic())
        return hit[1]
    data = None
    if v:
        try:
            data = yaml.safe_load(v.content)
        except yaml.YAMLError:
            data = None  # битая версия в базе не должна отключить правила — берём файл
    if data is None:
        data = _load(REPO_FILES[file_type])
    _memo[file_type] = (key, data, time.monotonic())
    return data


@lru_cache
def _drug_files() -> dict:
    return {d["code"]: d for d in (_load(p) for p in sorted((DATA_DIR / "drugs").glob("*.yaml")))}


def drugs() -> dict:
    out = dict(_drug_files())
    out["aviandr"] = versioned("aviandr")
    return out


@lru_cache
def scales() -> dict:
    out = {s["code"]: s for s in (_load(p) for p in sorted((DATA_DIR / "scales").glob("*.yaml")))}
    for s in out.values():  # PHQ-2 / GAD-2 — первые вопросы полной шкалы, формулировки не дублируем
        src = out.get(s.get("derived_from"))
        if src:
            s.setdefault("item_labels", src["item_labels"][: s["items"]])
            s.setdefault("answer_labels", src["answer_labels"])
            s.setdefault("period", src.get("period"))
    return out


@lru_cache
def drug_classes() -> dict:
    return {k.lower(): v for k, v in _load(DATA_DIR / "drug_classes.yaml").items()}


@lru_cache
def clinic_services() -> list:
    p = DATA_DIR / "clinic_services.yaml"
    return _load(p)["services"] if p.exists() else []


def routing_rules() -> list:
    return versioned("routing")["rules"]


@lru_cache
def trade_names() -> dict:
    p = DATA_DIR.parent.parent / "extraction" / "data" / "drug_names.yaml"
    return {k.lower(): v for k, v in _load(p).items()} if p.exists() else {}


def reload():
    """Сброс кеша (после загрузки правил через админку; другие процессы заметят новую версию сами)."""
    for fn in (_drug_files, scales, drug_classes, trade_names, clinic_services):
        fn.cache_clear()
    _memo.clear()
