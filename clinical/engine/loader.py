"""Загрузка справочников из YAML. Новые шкалы/препараты добавляются файлом, без правки кода."""
from functools import lru_cache
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def drugs() -> dict:
    return {d["code"]: d for d in (_load(p) for p in sorted((DATA_DIR / "drugs").glob("*.yaml")))}


@lru_cache
def scales() -> dict:
    return {s["code"]: s for s in (_load(p) for p in sorted((DATA_DIR / "scales").glob("*.yaml")))}


@lru_cache
def drug_classes() -> dict:
    return {k.lower(): v for k, v in _load(DATA_DIR / "drug_classes.yaml").items()}


@lru_cache
def routing_rules() -> list:
    return _load(DATA_DIR / "routing_rules.yaml")["rules"]


@lru_cache
def trade_names() -> dict:
    p = DATA_DIR.parent.parent / "extraction" / "data" / "drug_names.yaml"
    return {k.lower(): v for k, v in _load(p).items()} if p.exists() else {}


def reload():
    for fn in (drugs, scales, drug_classes, routing_rules, trade_names):
        fn.cache_clear()
