"""
Сквозные сценарии в браузере (Playwright) + метрики эргономики.

Для каждого сценария считаем:
- actions — число действий пользователя (клик, ввод в поле, выбор);
- keystrokes — сколько символов набрано;
- klm_s — оценка времени опытного пользователя по модели KLM (Card, Moran, Newell, 1980):
  клик P+B = 1,1 с; символ K = 0,28 с; обдумывание M = 1,35 с перед каждым действием;
- wall_ms — фактическое время прохождения автоматом (проверка, что интерфейс не тормозит).

  python e2e/scenarios.py --base http://127.0.0.1:8765 --out e2e/report.json [--video e2e/video]
Перед запуском: свежие демо-данные (python manage.py seed_demo --reset).
"""
import os
import argparse
import json
import time

from playwright.sync_api import sync_playwright

PWD = "Demo-2026!"
K, PB, M = 0.28, 1.1, 1.35


class Meter:
    def __init__(self, page, name):
        self.p, self.name, self.actions, self.keys = page, name, 0, 0
        self.t0 = time.perf_counter()

    def click(self, sel):
        self.actions += 1
        self.p.click(sel)

    def fill(self, sel, text):
        self.actions += 1
        self.keys += len(text)
        self.p.fill(sel, text)

    def check(self, sel):
        self.actions += 1
        self.p.check(sel)

    def result(self, **extra):
        klm = self.actions * (PB + M) + self.keys * K
        return {"scenario": self.name, "actions": self.actions, "keystrokes": self.keys, "klm_s": round(klm, 1),
                "wall_ms": round((time.perf_counter() - self.t0) * 1000), **extra}


def login(page, base, user):
    page.goto(base + "/login/")
    page.fill("#id_username", user)
    page.fill("#id_password", PWD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def run(base, video_dir=None, slow=0):
    results = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(slow_mo=slow, args=["--lang=ru-RU"], env={**os.environ, "LANG": "ru_RU.UTF-8", "LANGUAGE": "ru"})
        mob = dict(viewport={"width": 390, "height": 844}, locale="ru-RU", device_scale_factor=2, is_mobile=True, has_touch=True)
        desk = dict(viewport={"width": 1440, "height": 900}, locale="ru-RU")
        if video_dir:
            mob["record_video_dir"] = desk["record_video_dir"] = video_dir
            mob["record_video_size"] = {"width": 390, "height": 844}
            desk["record_video_size"] = {"width": 1440, "height": 900}

        # 1. Пациент: ежедневная отметка состояния
        ctx = br.new_context(**mob)
        pg = ctx.new_page()
        login(pg, base, "anna")
        m = Meter(pg, "Пациент: ежедневная отметка состояния")
        m.click("a[href='/me/checkin/']")
        m.click("label[for=m7]")
        m.click("label[for=a3]")
        m.fill("#sleep", "7")
        m.click("input[name=left_home][value='1'] >> xpath=..")
        m.click("input[name=ate_normally][value='1'] >> xpath=..")
        m.click("text=Сохранить запись")
        pg.wait_for_selector(".flash")
        results.append(m.result(ok=pg.is_visible("text=Запись сохранена")))

        # 2. Пациент: отметить приём таблетки
        m = Meter(pg, "Пациент: отметить приём препарата")
        pg.goto(base + "/me/")
        btn = pg.locator("button:has-text('Принял')").first
        if btn.count():
            m.actions += 1
            btn.click()
            pg.wait_for_selector(".state-yes")
        results.append(m.result(ok=pg.locator(".state-yes").count() > 0))

        # 3. Пациент: опросник GAD-7
        m = Meter(pg, "Пациент: опросник GAD-7 (7 вопросов)")
        pg.goto(base + "/me/scale/gad7/")
        for i in range(7):
            m.click(f"input[name=q{i}][value='1'] >> xpath=..")
        m.click("text=Отправить ответы")
        pg.wait_for_load_state("networkidle")
        results.append(m.result(ok="/me/progress/" in pg.url))
        ctx.close()

        # 4. Кризис: пациент пишет о нежелании жить -> экран помощи -> сигнал у врача
        dctx = br.new_context(**desk)
        dpg = dctx.new_page()
        login(dpg, base, "petrova")
        pctx = br.new_context(**mob)
        ppg = pctx.new_page()
        login(ppg, base, "anna")
        m = Meter(ppg, "Кризис: сообщение пациента -> экран помощи и сигнал врачу")
        ppg.goto(base + "/me/chat/")
        m.fill("#t", "Мне очень плохо, не хочу жить")
        t_send = time.perf_counter()
        m.click("text=Отправить")
        ppg.wait_for_selector(".crisis")
        dpg.goto(base + "/doctor/")
        dpg.wait_for_selector("text=Высказывания о суициде")
        alert_ms = round((time.perf_counter() - t_send) * 1000)
        results.append(m.result(ok=True, crisis_screen=True, alert_visible_to_doctor_ms=alert_ms))
        pctx.close()

        # 5. Психиатр: открыть сигнал, отметить реакцию
        m = Meter(dpg, "Психиатр: отреагировать на срочный сигнал")
        m.click(".alert-summary.critical .text-action")
        m.fill(".sig.critical input[name=resolution]", "Позвонила, договорились о приёме завтра")
        m.click(".sig.critical button")
        dpg.wait_for_selector(".flash")
        results.append(m.result(ok=dpg.is_visible("text=Сигнал отмечен")))

        # 6. Психиатр: новый пациент с ГТР -> назначение Авиандра (зелёный)
        m = Meter(dpg, "Психиатр: новый пациент с ГТР и назначение Авиандра")
        m.click("a[href='/doctor/patient/new/']")
        m.fill("#id_last_name", "Тестова")
        m.fill("#id_first_name", "Ольга")
        m.fill("#id_birth_date", "1990-04-12")
        m.actions += 1
        dpg.select_option("#id_sex", "F")
        m.fill("#id_icd10", "F41.1")
        m.fill("#id_egfr", "96")
        m.click("text=Создать карточку и получить код")
        dpg.wait_for_selector("text=Противопоказаний и взаимодействий не найдено")
        m.click("text=Назначить Авиандр")
        dpg.wait_for_selector("text=Авиандр назначен")
        results.append(m.result(ok=True, verdict="green"))

        # 7. Психиатр: противопоказание (Кузнецова) — система не даёт назначить без решения врача
        m = Meter(dpg, "Психиатр: попытка назначить при противопоказаниях")
        dpg.goto(base + "/doctor/")
        m.click("text=Кузнецова")
        dpg.wait_for_selector("text=Есть противопоказания по инструкции")
        m.click("text=Назначить Авиандр")
        dpg.wait_for_selector(".flash.error")
        blocked = dpg.is_visible("text=Есть противопоказания по инструкции. Назначение возможно только")
        results.append(m.result(ok=blocked, verdict="red", blocked_without_override=blocked))

        # 8. Психиатр: загрузка выписки и перенос в карточку
        m = Meter(dpg, "Психиатр: загрузка выписки из другой клиники")
        dpg.goto(base + "/doctor/")
        m.click("text=Павлов")
        m.click("text=Загрузить выписку")
        text = open("extraction/data/histories/test/case_13.txt", encoding="utf-8").read()
        m.actions += 1  # вставка из буфера — одно действие
        dpg.fill("#id_text", text)
        m.click("text=Разобрать текст")
        dpg.wait_for_selector(".import-review")
        for field in dpg.locator('.import-date-fields input[type=date]').all():
            field.fill("2026-09-01")
        m.click("text=Перенести отмеченное в карточку")
        dpg.wait_for_selector("text=Сведения из выписки перенесены")
        results.append(m.result(ok=True))
        dctx.close()

        # 9. Терапевт: пациент с суицидальным риском -> направление к психиатру
        tctx = br.new_context(**desk)
        tpg = tctx.new_page()
        login(tpg, base, "sidorov")
        m = Meter(tpg, "Терапевт: оформить обязательное направление к психиатру")
        m.click("text=Морозов")
        tpg.wait_for_selector("text=Обязательно")
        m.click(".sig.mandatory button[value=confirm]")
        tpg.wait_for_selector("text=Направление к психиатру подтверждено")
        results.append(m.result(ok=True))
        tctx.close()
        br.close()
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", default="e2e/report.json")
    ap.add_argument("--video")
    ap.add_argument("--slow", type=int, default=0)
    a = ap.parse_args()
    res = run(a.base, a.video, a.slow)
    for r in res:
        print(f"{'OK ' if r['ok'] else 'FAIL'} {r['scenario']}: действий {r['actions']}, ~{r['klm_s']} с (KLM), автомат {r['wall_ms']} мс"
              + (f", сигнал у врача через {r['alert_visible_to_doctor_ms']} мс" if 'alert_visible_to_doctor_ms' in r else ""))
    json.dump(res, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
