"""Скриншоты для слайдов и постера: python e2e/screens.py --base http://127.0.0.1:8765 --out docs/screens"""
import os
import argparse
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser(); ap.add_argument("--base", default="http://127.0.0.1:8765"); ap.add_argument("--out", default="docs/screens")
a = ap.parse_args(); B, O = a.base, a.out


def login(ctx, u):
    p = ctx.new_page(); p.goto(B + "/login/"); p.fill("#id_username", u); p.fill("#id_password", "Demo-2026!")
    p.click("button[type=submit]"); p.wait_for_load_state("networkidle"); return p


with sync_playwright() as pw:
    br = pw.chromium.launch(args=["--lang=ru-RU"], env={**os.environ, "LANG": "ru_RU.UTF-8", "LANGUAGE": "ru"})
    d = br.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU", device_scale_factor=2)
    p = login(d, "sidorov")
    p.screenshot(path=f"{O}/01_spisok_terapevt.png")
    p.click("text=Морозов"); p.wait_for_timeout(700); p.screenshot(path=f"{O}/02_kartochka_kriticheskiy.png", full_page=True)
    p.locator("#h-route").scroll_into_view_if_needed(); p.locator("#h-route").locator("xpath=..").screenshot(path=f"{O}/03_obyazatelno.png")
    p = login(br.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU", device_scale_factor=2), "petrova")
    p.screenshot(path=f"{O}/04_spisok_psihiatr.png")
    for name, f in [("Смирнова", "05_aviandr_zelenyy"), ("Кузнецова", "07_aviandr_krasnyy")]:
        p.goto(B + "/doctor/"); p.click(f"text={name}"); p.wait_for_timeout(700)
        p.screenshot(path=f"{O}/{f}_kartochka.png", full_page=True)
        p.locator("#h-av").locator("xpath=..").screenshot(path=f"{O}/{f}.png")
    p = login(br.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU", device_scale_factor=2), "sidorov")
    p.click("text=Волков"); p.wait_for_timeout(700); p.locator("#h-av").locator("xpath=..").screenshot(path=f"{O}/06_aviandr_zheltyy.png")
    m = br.new_context(viewport={"width": 390, "height": 844}, locale="ru-RU", device_scale_factor=3, is_mobile=True)
    p = login(m, "anna")
    p.screenshot(path=f"{O}/08_pacient_segodnya.png")
    p.goto(B + "/me/checkin/"); p.screenshot(path=f"{O}/09_pacient_otmetka.png")
    p.goto(B + "/me/progress/"); p.wait_for_timeout(600); p.screenshot(path=f"{O}/10_pacient_dinamika.png")
    p.goto(B + "/me/help/?crisis=1"); p.screenshot(path=f"{O}/11_pacient_pomosch.png")
    br.close()
print("ok")
