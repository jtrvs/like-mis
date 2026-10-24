"""
Запись демо-видео с подписями: python e2e/demo_video.py --base http://127.0.0.1:8765 --out e2e/demo.mp4
Перед записью: свежие демо-данные (python manage.py seed_demo --reset).
"""
import os
import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

PWD = "Demo-2026!"
CAPTION_JS = """(t) => {
  let el = document.getElementById('demo-cap');
  if (!el) { el = document.createElement('div'); el.id = 'demo-cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;pointer-events:none;transform:translateX(-50%);z-index:99999;max-width:86%;' +
      'background:rgba(30,42,50,.94);color:#fff;font:600 20px Golos,system-ui,sans-serif;padding:12px 20px;border-radius:10px;text-align:center;line-height:1.35';
    document.body.appendChild(el); }
  el.textContent = t; }"""


class Rec:
    def __init__(self, br, tmp, mobile=False):
        size = {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
        self.ctx = br.new_context(viewport=size, locale="ru-RU", record_video_dir=tmp, record_video_size=size,
                                  device_scale_factor=1, is_mobile=mobile, has_touch=mobile)
        self.p = self.ctx.new_page()
        self.mobile = mobile

    def cap(self, text, wait=2600):
        self.p.evaluate(CAPTION_JS, text)
        self.p.wait_for_timeout(wait)

    def go(self, url):
        self.p.goto(url)
        self.p.wait_for_load_state("networkidle")

    def login(self, base, user):
        self.go(base + "/login/")
        self.p.fill("#id_username", user)
        self.p.fill("#id_password", PWD)
        self.p.click("button[type=submit]")
        self.p.wait_for_load_state("networkidle")

    def scroll_to(self, sel):
        self.p.locator(sel).first.scroll_into_view_if_needed()
        self.p.wait_for_timeout(400)

    def close(self):
        path = self.p.video.path()
        self.ctx.close()
        return path


def main(base, out):
    tmp = tempfile.mkdtemp()
    clips = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(slow_mo=120, args=["--lang=ru-RU"], env={**os.environ, "LANG": "ru_RU.UTF-8", "LANGUAGE": "ru"})

        r = Rec(br, tmp)
        r.login(base, "sidorov")
        r.cap("Терапевт поликлиники. Пациенты отсортированы по риску: сверху — кому нужна помощь сейчас", 4200)
        r.cap("Срочные сигналы система нашла сама — по шкале PHQ-9 и по словам пациента в дневнике", 4200)
        r.p.click("text=Морозов")
        r.p.wait_for_load_state("networkidle")
        r.cap("Карточка пациента: сигналы, консультация психиатра, безопасность терапии, динамика", 4000)
        r.scroll_to("#h-route")
        r.cap("«Обязательно» — сплошной красный блок, «желательно» — пунктирный янтарный. Каждый вывод — с источником", 4600)
        r.scroll_to("#h-av")
        r.cap("Авиандр: диагноз F41.2 вне показаний инструкции, сахарный диабет — с осторожностью", 4200)
        r.scroll_to("#h-route")
        r.p.click(".sig.mandatory button[value=confirm]")
        r.p.wait_for_load_state("networkidle")
        r.cap("Решение принимает только врач: направление оформлено и записано в журнал решений", 3600)
        clips.append(r.close())

        r = Rec(br, tmp)
        r.login(base, "petrova")
        r.p.click("text=Смирнова")
        r.p.wait_for_load_state("networkidle")
        r.p.wait_for_timeout(600)
        r.cap("Психиатр. Анна, ГТР, 4 недели на Авиандре: GAD-7 снизился с 16 до 7, приём препарата 93%", 4600)
        r.go(base + "/doctor/")
        r.p.click("text=Кузнецова")
        r.p.wait_for_load_state("networkidle")
        r.scroll_to("#h-av")
        r.cap("Мария: печёночная недостаточность и ингибитор МАО, отменённый 5 дней назад — противопоказания по инструкции", 4800)
        r.p.click("text=Назначить Авиандр")
        r.p.wait_for_load_state("networkidle")
        r.cap("Система не даёт назначить молча: только как осознанное решение врача вне инструкции, с обоснованием", 4400)
        clips.append(r.close())

        r = Rec(br, tmp, mobile=True)
        r.login(base, "anna")
        r.cap("Кабинет пациента — с телефона, ставится как приложение", 3200)
        btn = r.p.locator("button:has-text('Принял')").first
        if btn.count():
            btn.click()
            r.cap("Приём таблетки — одно нажатие", 2600)
        r.p.click("text=Как вы сегодня?")
        r.p.wait_for_load_state("networkidle")
        r.p.click("label[for=m6]")
        r.p.click("label[for=a4]")
        r.p.fill("#sleep", "7")
        r.cap("Ежедневная отметка — около 20 секунд", 2400)
        r.p.click("text=Сохранить запись")
        r.p.wait_for_load_state("networkidle")
        r.go(base + "/me/chat/")
        r.p.fill("#t", "Мне очень плохо, не хочу жить")
        r.cap("Пациентке стало хуже. Она пишет врачу…", 2600)
        r.p.click("text=Отправить")
        r.p.wait_for_load_state("networkidle")
        r.cap("Сразу — экран поддержки: близкие люди, линии помощи, 112", 4400)
        clips.append(r.close())

        r = Rec(br, tmp)
        r.login(base, "petrova")
        r.cap("Через доли секунды сигнал уже у врача (и дублируется в мессенджер без персональных данных)", 4400)
        r.p.fill(".sig.critical input[name=resolution]", "Позвонила, приём завтра в 10:00")
        r.p.click(".sig.critical button")
        r.p.wait_for_load_state("networkidle")
        r.cap("Нет отметки за 15 минут — повтор, затем передача резервному врачу", 3600)
        r.p.click("text=Павлов")
        r.p.click("text=Загрузить выписку")
        r.p.fill("#id_text", open("extraction/data/histories/test/case_13.txt", encoding="utf-8").read())
        r.cap("Выписка из другой клиники в любом формате…", 2400)
        r.p.click("text=Разобрать текст")
        r.p.wait_for_load_state("networkidle")
        r.cap("…разобрана за миллисекунды: диагнозы, препараты, шкалы, факторы риска. Точность F1 = 0,94", 4800)
        clips.append(r.close())
        br.close()

    # склейка: мобильные клипы — по центру кадра 1440×900
    parts = []
    for i, c in enumerate(clips):
        o = Path(tmp) / f"p{i}.mp4"
        vf = "scale=-2:900,pad=1440:900:(ow-iw)/2:0:color=0x1E2A32" if "390" in subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0", c], capture_output=True, text=True).stdout else "scale=1440:900"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", c, "-vf", vf + ",fps=25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(o)], check=True)
        parts.append(o)
    lst = Path(tmp) / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", out], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print("видео:", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", default="e2e/demo.mp4")
    a = ap.parse_args()
    main(a.base, a.out)
