"""
Оценка точности извлечения против эталонной разметки.

  python extraction/evaluate.py extraction/data/histories/test
  python extraction/evaluate.py extraction/data/histories/test --json report.json --verbose

Метрики:
- diagnoses: precision / recall / F1 (строго по коду и по рубрике из 3 знаков)
- medications: P/R/F1 по МНН; на совпавших — точность дозы и кратности
- stopped_medications, conditions: P/R/F1
- scales: P/R/F1 по паре (шкала, балл)
- скалярные поля: доля верных
- micro-F1 по всем «единицам фактов» и время обработки документа
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extraction.engine import rules_extractor  # noqa: E402
from extraction.engine.rules_extractor import extract  # noqa: E402

rules_extractor.TODAY = __import__("datetime").date(2026, 10, 20)  # возраст в эталоне посчитан на эту дату


class PRF:
    def __init__(self):
        self.tp = self.fp = self.fn = 0

    def add(self, pred: set, gold: set):
        self.tp += len(pred & gold)
        self.fp += len(pred - gold)
        self.fn += len(gold - pred)

    @property
    def p(self):
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def r(self):
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self):
        return 2 * self.p * self.r / (self.p + self.r) if self.p + self.r else 0.0

    def row(self):
        return {"precision": round(self.p, 3), "recall": round(self.r, 3), "f1": round(self.f1, 3), "tp": self.tp, "fp": self.fp, "fn": self.fn}


def close(a, b, tol=0.01):
    if a is None or b is None:
        return a == b
    return abs(float(a) - float(b)) <= tol * max(1, abs(float(b)))


def run(folder: Path, verbose=False):
    sets = {k: PRF() for k in ("diagnoses", "diagnoses_3char", "medications", "stopped_medications", "conditions", "scales")}
    scalar = {k: [0, 0] for k in ("sex", "age", "suicidal_risk", "pregnant", "breastfeeding", "egfr", "child_pugh")}
    dose_ok = times_ok = matched = 0
    total = PRF()
    times = []
    errors = []
    files = sorted(folder.glob("*.txt"))
    for txt in files:
        gold = json.loads(txt.with_suffix(".json").read_text(encoding="utf-8"))
        t0 = time.perf_counter()
        pred = extract(txt.read_text(encoding="utf-8"))
        times.append((time.perf_counter() - t0) * 1000)
        doc_err = []

        def cmp_set(name, p, g):
            sets[name].add(p, g)
            total.add({(name, x) for x in p}, {(name, x) for x in g})
            if p != g:
                doc_err.append(f"{name}: лишнее {sorted(p - g)} пропущено {sorted(g - p)}")

        cmp_set("diagnoses", set(pred["diagnoses"]), set(gold["diagnoses"]))
        sets["diagnoses_3char"].add({c[:3] for c in pred["diagnoses"]}, {c[:3] for c in gold["diagnoses"]})
        pm = {m["inn"]: m for m in pred["medications"]}
        gm = {m["inn"]: m for m in gold["medications"]}
        cmp_set("medications", set(pm), set(gm))
        for inn in set(pm) & set(gm):
            matched += 1
            d_ok = close(pm[inn]["dose_mg"], gm[inn]["dose_mg"])
            t_ok = pm[inn]["times_per_day"] == gm[inn]["times_per_day"]
            dose_ok += d_ok
            times_ok += t_ok
            if not (d_ok and t_ok):
                doc_err.append(f"{inn}: доза {pm[inn]['dose_mg']} vs {gm[inn]['dose_mg']}, кратность {pm[inn]['times_per_day']} vs {gm[inn]['times_per_day']}")
        cmp_set("stopped_medications", set(pred["stopped_medications"]), set(gold["stopped_medications"]))
        cmp_set("conditions", set(pred["conditions"]), set(gold["conditions"]))
        cmp_set("scales", set(pred["scales"].items()), set(gold["scales"].items()))
        for k in scalar:
            ok = close(pred[k], gold[k], 0.0) if k in ("egfr", "age") else pred[k] == gold[k]
            scalar[k][0] += ok
            scalar[k][1] += 1
            nv = lambda v: str(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
            total.add({(k, nv(pred[k]))} if pred[k] not in (None, False) else set(), {(k, nv(gold[k]))} if gold[k] not in (None, False) else set())
            if not ok:
                doc_err.append(f"{k}: {pred[k]!r} vs эталон {gold[k]!r}")
        if doc_err:
            errors.append((txt.name, doc_err))

    report = {
        "documents": len(files),
        "fields": {k: v.row() for k, v in sets.items()},
        "dose_accuracy": round(dose_ok / matched, 3) if matched else None,
        "frequency_accuracy": round(times_ok / matched, 3) if matched else None,
        "scalar_accuracy": {k: round(v[0] / v[1], 3) for k, v in scalar.items()},
        "micro": total.row(),
        "ms_per_document": {"median": round(statistics.median(times), 2), "max": round(max(times), 2)},
    }
    return report, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    report, errors = run(a.folder, a.verbose)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if a.verbose:
        for name, errs in errors:
            print(f"\n--- {name}")
            for e in errs:
                print("  ", e)
    if a.json:
        a.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
