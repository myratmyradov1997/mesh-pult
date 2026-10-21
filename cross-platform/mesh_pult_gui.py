#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт МЭШ — кроссплатформенная версия (Windows / Linux / macOS без чёлки).

Окно со списком уроков дня: клик по уроку → ученики → 5 4 3 2 Н.
Уже выставленные оценки и Н подсвечены; повторный клик по подсвеченной кнопке
удаляет запись; «↩ Отменить» удаляет последнее, что поставил пульт.
«☑ Перекличка» — отметить отсутствующих и отправить Н одним пакетом.

Токен берётся из cookies браузера (нужно один раз войти в https://school.mos.ru),
Profile-Id — из Local Storage браузера. Вся работа с API — в mesh_remote.py
(те же функции, что у Mac-версии).

Ключ --selftest: отрисовать окно на тестовых данных без сети и выйти (для CI).
"""
import os
import sys
import re
import json
import base64
import datetime
import threading
import queue
import webbrowser

VERSION = "1.1.0"
REPO = "myratmyradov1997/mesh-pult"

# PyInstaller --windowed: stdout/stderr отсутствуют, а v3 печатает логи
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tkinter as tk
from tkinter import ttk, messagebox

C_BG = "#191b1f"
C_PANEL = "#22252b"
C_ROW = "#2e323a"
C_ROW_CUR = "#3a4048"
C_TEXT = "#e8eaf0"
C_DIM = "#8a909a"
C_ACCENT = "#2a9d94"   # оценка на активном уроке
C_PURPLE = "#7c5fd6"   # оценка на соседнем уроке двойного
C_ORANGE = "#c27a12"   # Н
C_YELLOW = "#b59a1c"   # запрос в пути
C_OK = "#3ecfc3"
C_ERR = "#f87171"

FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_S = ("Segoe UI", 9)

TOKEN_FILE = os.path.expanduser("~/.mesh_token")
REFRESH_FILE = os.path.expanduser("~/.mesh_refresh_token")
PROFILE_FILE = os.path.expanduser("~/.mesh_profile_id")
SETTINGS_FILE = os.path.expanduser("~/.mesh_pult_settings.json")


# ---------- токен и профиль ----------

def jwt_data(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def token_valid(token):
    try:
        exp = jwt_data(token).get("exp")
        return bool(exp and exp > datetime.datetime.now().timestamp())
    except Exception:
        return False


def read_token():
    tok = os.environ.get("MESH_TOKEN")
    if tok and token_valid(tok):
        return tok
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
        if t and token_valid(t):
            return t
    return None


def token_left_s(token):
    try:
        return jwt_data(token).get("exp", 0) - datetime.datetime.now().timestamp()
    except Exception:
        return 0


def try_get_token():
    """Цепочка: файл → refresh → браузер (без интерактивных prompt).

    Токен, которому осталось меньше 30 минут, заранее продлеваем через refresh.
    """
    import mesh_automation_v3 as v3
    tok = read_token()
    if tok and (token_left_s(tok) > 30 * 60 or not os.path.exists(REFRESH_FILE)):
        return tok, None
    try:
        tok = v3.refresh_mesh_token()
        if tok:
            return tok, None
    except Exception as e:
        print("refresh ошибка:", e, file=sys.stderr)
    if tok:
        return tok, None   # refresh не удался, но старый ещё действует
    tok = token_from_browsers()
    if tok:
        return tok, None
    try:
        tok = v3.get_token_from_browser()   # macOS: Helium/Chrome
        if tok:
            try:
                open(TOKEN_FILE, "w").write(tok)
                os.chmod(TOKEN_FILE, 0o600)
            except Exception:
                pass
            return tok, None
    except Exception as e:
        print("cookie ошибка:", e, file=sys.stderr)
    return None, "нет входа"


def token_from_browsers():
    """aupd_token из cookies установленных браузеров; aupd_refresh_token — чтобы продлевать вход.

    Новый Chrome/Edge на Windows может шифровать cookies так, что их не прочитать, —
    тогда остаётся Firefox или ручной вход (окно «Вход в МЭШ»).
    """
    try:
        import browser_cookie3 as bc
    except Exception:
        return None
    for name in ("chrome", "edge", "yandex", "firefox", "chromium", "brave", "opera", "vivaldi", "librewolf"):
        fn = getattr(bc, name, None)
        if not fn:
            continue
        try:
            jar = list(fn(domain_name="mos.ru"))
        except Exception as e:
            print(f"cookies {name}: {e}", file=sys.stderr)
            continue
        access = next((c.value for c in jar if c.name == "aupd_token" and token_valid(c.value)), None)
        refresh = next((c.value for c in jar if c.name == "aupd_refresh_token" and c.value), None)
        if access:
            try:
                open(TOKEN_FILE, "w").write(access)
                os.chmod(TOKEN_FILE, 0o600)
                if refresh:
                    open(REFRESH_FILE, "w").write(refresh)
                    os.chmod(REFRESH_FILE, 0o600)
            except Exception:
                pass
            return access
    return None


def save_login(value):
    """Ручной вход: значение cookie aupd_refresh_token (или aupd_token) из браузера.

    Сначала пробуем как refresh-токен (тогда пульт дальше продлевает вход сам),
    иначе — как готовый токен доступа.
    """
    import mesh_automation_v3 as v3
    value = value.strip().strip('"')
    open(REFRESH_FILE, "w").write(value)
    os.chmod(REFRESH_FILE, 0o600)
    if v3.refresh_mesh_token():
        return
    os.remove(REFRESH_FILE)
    if value.count(".") == 2 and token_valid(value):
        open(TOKEN_FILE, "w").write(value)
        os.chmod(TOKEN_FILE, 0o600)
        return
    raise RuntimeError("значение не подошло — скопируйте aupd_refresh_token целиком")


def read_profile():
    pid = os.environ.get("MESH_PROFILE_ID")
    if pid:
        return pid
    if os.path.exists(PROFILE_FILE):
        p = open(PROFILE_FILE).read().strip()
        if p:
            return p
    auto = find_profile_id_storage()
    if auto:
        save_profile(auto)
        return str(auto)
    return None


def save_profile(pid):
    try:
        open(PROFILE_FILE, "w").write(str(pid).strip())
        os.chmod(PROFILE_FILE, 0o600)
    except Exception:
        pass


def find_profile_id_storage():
    """Profile-Id из Local Storage браузера: school.mos.ru хранит там profile_id."""
    import shutil
    import tempfile
    import glob
    rootdirs = [
        os.environ.get("LOCALAPPDATA"),  # Windows: Chrome/Edge/Yandex
        os.path.expanduser("~/.config"),  # Linux
        os.path.expanduser("~/Library/Application Support"),  # macOS
    ]
    browsers = [
        "Google/Chrome", "Microsoft/Edge", "Chromium", "chromium", "google-chrome",
        "microsoft-edge", "net.imput.helium", "BraveSoftware/Brave-Browser",
        "Yandex/YandexBrowser", "yandex-browser", "Vivaldi", "vivaldi",
    ]
    found = []
    for rootdir in rootdirs:
        if not rootdir:
            continue
        for b in browsers:
            for sub in (os.path.join(rootdir, b, "User Data", "Default", "Local Storage", "leveldb"),
                        os.path.join(rootdir, b, "Default", "Local Storage", "leveldb"),
                        os.path.join(rootdir, b, "Local Storage", "leveldb")):
                if not os.path.isdir(sub):
                    continue
                tmp = tempfile.mkdtemp(prefix="meshpult_ls_")
                try:
                    for f in glob.glob(os.path.join(sub, "*.ldb")) + glob.glob(os.path.join(sub, "*.log")):
                        try:
                            dst = os.path.join(tmp, os.path.basename(f))
                            shutil.copy(f, dst)
                            data = open(dst, "rb").read()
                        except Exception:
                            continue
                        for m in re.finditer(rb'"profile_id"\s*:\s*(\d{6,12})', data):
                            found.append(int(m.group(1)))
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
    if not found:
        return None
    return max(set(found), key=found.count)


def load_settings():
    try:
        return json.load(open(SETTINGS_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_settings(s):
    try:
        json.dump(s, open(SETTINGS_FILE, "w", encoding="utf-8"))
    except Exception:
        pass


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


# ---------- окно ----------

class PultApp:
    def __init__(self, root, offline=False):
        self.root = root
        self.offline = offline
        self.q = queue.Queue()
        self.v3 = None
        self.remote = None
        self.mesh = None
        self.lessons = []
        self.students = {}       # group_id → [{id, name}]
        self.grades = {}         # lesson_id → student_id → ["5", …]
        self.mark_ids = {}       # lesson_id → student_id → [{id, value}]
        self.absences = {}       # lesson_id → student_id → 1 (Н) | 2 (УП)
        self.absence_ids = {}    # lesson_id → student_id → id отметки
        self.expanded = set()
        self.block_choice = {}   # ключ блока → индекс активного урока в блоке
        self.roll = {}           # ключ блока → отмеченные на перекличке
        self.history = []        # поставленное пультом: {kind, ids, lesson_id, title}
        self.in_flight = set()
        self.cache_day = ""
        self.token_exp = ""
        self.result = ""
        self.result_err = False
        self.update = None       # {"version", "url"} если на GitHub есть новее
        self.settings = load_settings()

        root.title(f"Пульт МЭШ {VERSION}")
        root.geometry(self.settings.get("geometry") or "600x760")
        root.minsize(480, 400)
        root.configure(bg=C_BG)
        self._set_icon()
        root.attributes("-topmost", bool(self.settings.get("topmost")))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self._pump()
        if not offline:
            root.after(300, self.refresh_all)
            root.after(1500, self.check_update)
            root.after(5 * 60 * 1000, self._periodic)

    def _set_icon(self):
        base = getattr(sys, "_MEIPASS", _HERE)   # в сборке PyInstaller файлы лежат в _MEIPASS
        try:
            self._icon = tk.PhotoImage(file=os.path.join(base, "icon.png"))
            self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    # ---------- каркас ----------
    def _build(self):
        top = tk.Frame(self.root, bg=C_PANEL)
        top.pack(fill="x")
        self.status = tk.Label(top, text="Пульт МЭШ", bg=C_PANEL, fg=C_TEXT,
                               font=("Segoe UI", 11, "bold"), anchor="w")
        self.status.pack(side="left", padx=10, pady=8)
        bar = tk.Frame(self.root, bg=C_PANEL)
        bar.pack(fill="x")
        for text, cmd in (("Без д/з сейчас", self.do_nohw),
                          ("↩ Отменить", self.do_undo),
                          ("Обновить", self.refresh_all)):
            self._btn(bar, text, C_ROW, cmd).pack(side="left", padx=(8, 0), pady=(0, 8))
        self.topmost = tk.BooleanVar(value=bool(self.settings.get("topmost")))
        tk.Checkbutton(bar, text="Поверх окон", variable=self.topmost, command=self.toggle_topmost,
                       bg=C_PANEL, fg=C_TEXT, selectcolor=C_ROW, activebackground=C_PANEL,
                       activeforeground=C_TEXT, font=FONT_S, bd=0,
                       highlightthickness=0).pack(side="right", padx=8, pady=(0, 8))

        self.banner = tk.Frame(self.root, bg=C_BG)
        self.banner.pack(fill="x")

        mid = tk.Frame(self.root, bg=C_BG)
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg=C_BG, highlightthickness=0)
        self.scroll = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.body = tk.Frame(self.canvas, bg=C_BG)
        self.canvas.create_window((0, 0), window=self.body, anchor="nw", tags="body")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure("body", width=e.width))
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

        self.result_label = tk.Label(self.root, text="", bg=C_PANEL, fg=C_OK, font=FONT_S,
                                     anchor="w", justify="left", wraplength=560)
        self.result_label.pack(fill="x", ipady=6, ipadx=8)

    def _wheel(self, e):
        if e.delta:
            # Windows: ±120 за щелчок; macOS: мелкие значения
            step = -int(e.delta / 120) if abs(e.delta) >= 120 else (-1 if e.delta > 0 else 1)
            self.canvas.yview_scroll(step, "units")

    def _btn(self, parent, text, bg, cmd, fg=C_TEXT, width=None, font=FONT_S):
        b = tk.Button(parent, text=text, bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                      relief="flat", bd=0, padx=8, pady=3, font=font, command=cmd,
                      cursor="hand2", highlightthickness=0)
        if width:
            b.configure(width=width)
        return b

    def toggle_topmost(self):
        self.settings["topmost"] = bool(self.topmost.get())
        self.root.attributes("-topmost", self.settings["topmost"])
        save_settings(self.settings)

    def on_close(self):
        self.settings["geometry"] = self.root.geometry()
        save_settings(self.settings)
        self.root.destroy()

    # ---------- фоновые задачи ----------
    def run(self, fn, ok, err=None):
        """fn — в отдельном потоке; ok/err — в потоке окна через очередь."""
        def work():
            try:
                res = fn()
                self.q.put((ok, res))
            except Exception as e:
                self.q.put((err or self.fail, e))
        threading.Thread(target=work, daemon=True).start()

    def _pump(self):
        changed = False
        while True:
            try:
                cb, val = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                cb(val)
            except Exception as e:
                self.set_result(f"Ошибка окна: {type(e).__name__}: {e}", err=True)
            changed = True
        if changed:
            self.render()
        self.root.after(100, self._pump)

    def set_result(self, text, err=False):
        self.result = text
        self.result_err = err
        self.result_label.configure(text=text, fg=C_ERR if err else C_OK)

    def fail(self, e):
        self.set_result(f"⚠ {e}", err=True)

    # ---------- данные ----------
    def _connect(self):
        """Поток: токен + профиль → MeshAutomation."""
        import mesh_automation_v3 as v3
        import mesh_remote as remote
        self.v3, self.remote = v3, remote
        tok, err = try_get_token()
        if not tok:
            return "login"
        pid = read_profile()
        if not pid:
            return None
        return v3.MeshAutomation(bearer_token=tok, profile_id=str(pid))

    def refresh_all(self):
        self.status.configure(text="Загрузка…")

        def fn():
            mesh = self._connect()
            if mesh is None or mesh == "login":
                return mesh
            data = self.remote.today_data(mesh)
            data["token_expires"] = self.remote.token_expires(mesh)
            return mesh, data

        def ok(res):
            if res is None:
                self.status.configure(text="Нужен Profile-Id (один раз)")
                self.ask_profile()
                return
            if res == "login":
                self.status.configure(text="Нужен вход в МЭШ")
                self.ask_login()
                return
            self.mesh, data = res
            today = datetime.date.today().isoformat()
            if self.cache_day != today:
                self.cache_day = today
                for d in (self.students, self.grades, self.mark_ids, self.absences, self.absence_ids):
                    d.clear()
                self.expanded.clear()
                self.roll.clear()
                self.history.clear()
            self.lessons = data["lessons"]
            self.token_exp = data.get("token_expires") or ""
            self.preload()

        def err(e):
            self.status.configure(text="Нет связи с МЭШ")
            self.fail(e)

        self.run(fn, ok, err)

    def _periodic(self):
        self.refresh_all()
        self.root.after(5 * 60 * 1000, self._periodic)

    def preload(self):
        """Ученики всех групп и оценки/Н всех уроков дня — чтобы клик не ждал сеть."""
        for gid in {l["group_id"] for l in self.lessons if l.get("group_id")}:
            if gid not in self.students:
                self.load_students(gid)
        for l in self.lessons:
            if l.get("schedule_lesson_id"):
                self.load_lesson(l["schedule_lesson_id"])

    def load_students(self, gid):
        mesh = self.mesh

        def ok(d):
            self.students[gid] = d["students"]
        self.run(lambda: self.remote.students_data(mesh, gid), ok)

    def load_lesson(self, lid):
        mesh = self.mesh

        def ok(d):
            self.grades[lid] = d["grades"]
            self.mark_ids[lid] = d["mark_ids"]
            self.absences[lid] = d["absences"]
            self.absence_ids[lid] = d["absence_ids"]
        self.run(lambda: self.remote.grades_data(mesh, lid), ok)

    def ask_login(self):
        """Cookies браузера прочитать не удалось (так бывает с новым Chrome на Windows)."""
        if getattr(self, "_login_asked", False):
            self.set_result("Нет входа в МЭШ: войдите на school.mos.ru и нажмите «Обновить»", err=True)
            return
        self._login_asked = True
        import tkinter.simpledialog as sd
        v = sd.askstring(
            "Вход в МЭШ",
            "Пульт не смог взять вход из браузера.\n\n"
            "1. Откройте https://school.mos.ru и войдите в «Кабинет учителя».\n"
            "2. Нажмите F12 → вкладка «Application» (Приложение) → Cookies → https://school.mos.ru.\n"
            "3. Найдите строку aupd_refresh_token и скопируйте её значение (Value).\n\n"
            "Вставьте значение — дальше пульт будет продлевать вход сам:",
            parent=self.root)
        if v and v.strip():
            try:
                save_login(v)
            except Exception as e:
                self.fail(e)
                return
            self.refresh_all()
        else:
            self.set_result("Нет входа в МЭШ: войдите на school.mos.ru и нажмите «Обновить»", err=True)

    def ask_profile(self):
        import tkinter.simpledialog as sd
        pid = sd.askstring(
            "Профиль учителя",
            "Не удалось найти Profile-Id автоматически.\n\n"
            "Откройте school.mos.ru → «Журнал» → F12 → Network → любой запрос к api →\n"
            "в заголовках найдите Profile-Id и вставьте это число:",
            parent=self.root)
        if pid and pid.strip().isdigit():
            save_profile(pid.strip())
            self.refresh_all()

    def check_update(self):
        def fn():
            import requests
            r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=8)
            r.raise_for_status()
            return r.json()

        def ok(d):
            tag = d.get("tag_name") or ""
            if version_tuple(tag) > version_tuple(VERSION):
                self.update = {"version": tag, "url": d.get("html_url")}
                self.show_update()

        self.run(fn, ok, lambda e: None)   # нет сети — молча

    def show_update(self):
        for w in self.banner.winfo_children():
            w.destroy()
        if not self.update:
            return
        f = tk.Frame(self.banner, bg=C_PURPLE)
        f.pack(fill="x")
        tk.Label(f, text=f"Доступна новая версия {self.update['version']}", bg=C_PURPLE, fg=C_TEXT,
                 font=FONT_S).pack(side="left", padx=10, pady=5)
        self._btn(f, "Скачать", C_ROW, lambda: webbrowser.open(self.update["url"])).pack(
            side="right", padx=8, pady=4)

    # ---------- уроки и блоки ----------
    def blocks(self):
        """Подряд идущие уроки одной группы — один блок (двойной урок)."""
        out, i = [], 0
        while i < len(self.lessons):
            j = i + 1
            gid = self.lessons[i].get("group_id")
            while j < len(self.lessons) and gid and self.lessons[j].get("group_id") == gid:
                j += 1
            out.append((f"b{i}-{j - i}-{gid}", self.lessons[i:j]))
            i = j
        return out

    def active_idx(self, key, ls):
        if key in self.block_choice and self.block_choice[key] < len(ls):
            return self.block_choice[key]
        for k, l in enumerate(ls):
            if l.get("is_current"):
                return k
        return len(ls) - 1

    def absence_summary(self, ls):
        reasons = {}
        for l in ls:
            for sid, r in self.absences.get(l.get("schedule_lesson_id"), {}).items():
                reasons.setdefault(sid, r)
        up = sum(1 for r in reasons.values() if r == 2)
        n = len(reasons) - up
        return (f" · Н {n}" if n else "") + (f" · УП {up}" if up else "")

    # ---------- отрисовка ----------
    def render(self):
        y = self.canvas.yview()[0]
        for w in self.body.winfo_children():
            w.destroy()
        n = len(self.lessons)
        head = f"Уроков: {n}" if n else "Уроков сегодня нет"
        if any(l.get("is_current") for l in self.lessons):
            head += " · ● идёт урок"
        if self.token_exp:
            head += f" · вход до {self.token_exp}"
        if self.mesh or self.offline:
            self.status.configure(text=head)
        for key, ls in self.blocks():
            self.render_block(key, ls)
        self.body.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(y)

    def render_block(self, key, ls):
        cur = any(l.get("is_current") for l in ls)
        first, last = ls[0], ls[-1]
        t = ("● " if cur else "○ ") + f"{first.get('begin') or '?'}–{last.get('end') or '?'}  " \
            f"{first.get('subject', '?')} · {first.get('group', '?')}"
        if len(ls) > 1:
            t += f" · {len(ls)} урока"
        t += self.absence_summary(ls)
        bg = C_ROW_CUR if cur else C_ROW
        row = tk.Frame(self.body, bg=bg, padx=10, pady=7, cursor="hand2")
        row.pack(fill="x", padx=8, pady=3)
        lbl = tk.Label(row, text=t, bg=bg, fg=C_TEXT, font=FONT, anchor="w", justify="left",
                       wraplength=520, cursor="hand2")
        lbl.pack(fill="x")
        for w in (row, lbl):
            w.bind("<Button-1>", lambda e, k=key: self.toggle(k))
        if key not in self.expanded:
            return
        act = self.active_idx(key, ls)
        wrap = tk.Frame(self.body, bg=C_BG)
        wrap.pack(fill="x", padx=18, pady=(0, 8))
        if len(ls) > 1:
            r = tk.Frame(wrap, bg=C_BG)
            r.pack(anchor="w", pady=(2, 4))
            tk.Label(r, text="Отметки идут в:", bg=C_BG, fg=C_DIM, font=FONT_S).pack(side="left")
            for k, l in enumerate(ls):
                self._btn(r, f"{k + 1}-й ({l.get('begin') or '?'})", C_ACCENT if k == act else C_ROW,
                          lambda k=k: self.pick(key, k)).pack(side="left", padx=3)
        gid = first.get("group_id")
        st = self.students.get(gid)
        if st is None:
            tk.Label(wrap, text="Загружаю учеников…", bg=C_BG, fg=C_DIM, font=FONT_S).pack(anchor="w")
            return
        if not st:
            tk.Label(wrap, text="Ученики не найдены", bg=C_BG, fg=C_DIM, font=FONT_S).pack(anchor="w")
            return
        if key in self.roll:
            self.render_roll(wrap, key, ls, act, st)
        else:
            self._btn(wrap, "☑ Перекличка", C_ROW, lambda: self.roll_start(key)).pack(anchor="w", pady=(0, 4))
            self.render_students(wrap, key, ls, act, st)

    def render_students(self, wrap, key, ls, act, st):
        active = ls[act]
        alid = active.get("schedule_lesson_id")
        for s in st:
            sid = s["id"]
            reason = self.absences.get(alid, {}).get(sid)
            if reason is None:
                reason = next((self.absences.get(l.get("schedule_lesson_id"), {}).get(sid)
                               for l in ls if sid in self.absences.get(l.get("schedule_lesson_id"), {})), None)
            name = s["name"] if reason is None else f"{'УП' if reason == 2 else 'Н'} · {s['name']}"
            row = tk.Frame(wrap, bg=C_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=name, bg=C_BG, fg=C_DIM if reason else C_TEXT, font=FONT_S,
                     width=30, anchor="w").pack(side="left")
            for g in (5, 4, 3, 2, 0):
                kind = "absence" if g == 0 else "mark"
                if g:
                    bg = C_ROW
                    for k, l in enumerate(ls):
                        if str(g) in self.grades.get(l.get("schedule_lesson_id"), {}).get(sid, []):
                            bg = C_ACCENT if k == act else C_PURPLE
                            break
                else:
                    bg = C_ORANGE if any(sid in self.absences.get(l.get("schedule_lesson_id"), {}) for l in ls) else C_ROW
                if self.flight_key(kind, sid, alid, g or None) in self.in_flight:
                    bg = C_YELLOW
                self._btn(row, "Н" if g == 0 else str(g), bg,
                          lambda s=s, g=g: self.on_grade(key, s, g), width=2,
                          font=FONT_B).pack(side="left", padx=2)

    def render_roll(self, wrap, key, ls, act, st):
        alid = ls[act].get("schedule_lesson_id")
        marked = self.roll[key]
        hint = "Отметь отсутствующих" + (f" (урок {act + 1}-й)" if len(ls) > 1 else "")
        tk.Label(wrap, text=hint, bg=C_BG, fg=C_DIM, font=FONT_S).pack(anchor="w")
        for s in st:
            sid = s["id"]
            r = self.absences.get(alid, {}).get(sid)
            if r is not None:
                tk.Label(wrap, text=f"{'УП' if r == 2 else 'Н'} · {s['name']} — уже в журнале",
                         bg=C_BG, fg=C_DIM, font=FONT_S, anchor="w").pack(fill="x", pady=1)
                continue
            on = sid in marked
            self._btn(wrap, ("Н · " if on else "") + s["name"], C_ORANGE if on else C_ROW,
                      lambda sid=sid: self.roll_toggle(key, sid)).pack(fill="x", pady=1)
        r = tk.Frame(wrap, bg=C_BG)
        r.pack(anchor="w", pady=(4, 0))
        lids = [l.get("schedule_lesson_id") for l in ls if l.get("schedule_lesson_id")]
        self._btn(r, f"Отправить Н ({len(marked)})", C_ORANGE,
                  lambda: self.roll_submit(key, [alid])).pack(side="left", padx=(0, 4))
        if len(lids) > 1:
            self._btn(r, f"Н на все {len(lids)} урока", C_ORANGE,
                      lambda: self.roll_submit(key, lids)).pack(side="left", padx=4)
        self._btn(r, "Отмена", C_ROW, lambda: self.roll_cancel(key)).pack(side="left", padx=4)

    # ---------- действия ----------
    def toggle(self, key):
        if key in self.expanded:
            self.expanded.discard(key)
        else:
            self.expanded.add(key)
            ls = dict(self.blocks()).get(key) or []
            if self.mesh and ls:
                if ls[0].get("group_id") and ls[0]["group_id"] not in self.students:
                    self.load_students(ls[0]["group_id"])
                for l in ls:
                    if l.get("schedule_lesson_id") and l["schedule_lesson_id"] not in self.grades:
                        self.load_lesson(l["schedule_lesson_id"])
        self.render()

    def pick(self, key, k):
        self.block_choice[key] = k
        self.render()

    def flight_key(self, kind, sid, lid, grade=None):
        return f"{kind}-{sid}-{lid}" + (f"-{grade}" if grade else "")

    def on_grade(self, key, s, g):
        ls = dict(self.blocks()).get(key)
        if not ls:
            return
        lid = ls[self.active_idx(key, ls)].get("schedule_lesson_id")
        sid, name = s["id"], s["name"]
        if g:
            # повторный клик по уже выставленной оценке на этом уроке — удалить её
            same = [r for r in self.mark_ids.get(lid, {}).get(sid, []) if r["value"] == str(g)]
            if same:
                self.delete("mark", [same[-1]["id"]], lid, f"{g} {name}")
            else:
                self.perform("mark", [sid], g, [lid], f"{g} {name}")
            return
        reason = self.absences.get(lid, {}).get(sid)
        if reason == 2:
            self.set_result(f"УП у {name} снимается только в журнале МЭШ")
        elif reason == 1:
            aid = self.absence_ids.get(lid, {}).get(sid)
            if aid:
                self.delete("absence", [aid], lid, f"Н {name}")
            else:
                self.set_result(f"Н {name}: обновляю данные урока, нажми ещё раз")
                self.load_lesson(lid)
        else:
            self.perform("absence", [sid], None, [lid], f"Н {name}")

    def perform(self, kind, sids, grade, lids, title):
        if not self.mesh:
            self.set_result("Нет связи с МЭШ — нажми «Обновить»", err=True)
            return
        mesh, v3, remote = self.mesh, self.v3, self.remote
        for lid in (lids or [None]):
            keys = {self.flight_key(kind, sid, lid, grade) for sid in sids}
            if keys & self.in_flight:
                continue   # двойной клик, запрос ещё идёт
            self.in_flight |= keys
            self.set_result(f"выполняю {title}…")

            def fn(lid=lid):
                return remote.act_data(v3, mesh, kind,
                                       student_id=sids[0] if len(sids) == 1 else None,
                                       grade=grade, schedule_lesson_id=lid,
                                       student_ids=sids if len(sids) > 1 else None)

            def ok(d, lid=lid, keys=keys):
                self.in_flight -= keys
                if d.get("error"):
                    self.set_result(f"⚠ {title}: {d['error']}", err=True)
                else:
                    failed = len(d.get("failed") or {})
                    self.set_result(f"✓ {title} — готово" if not failed
                                    else f"⚠ {title} — не прошло у {failed} уч.", err=bool(failed))
                    created = [i for i in (d.get("created") or {}).values() if i]
                    real_lid = (d.get("lesson") or {}).get("schedule_lesson_id") or lid
                    if created and kind != "nohomework":
                        self.history.append({"kind": kind, "ids": created, "lesson_id": real_lid, "title": title})
                    if kind == "absence" and real_lid:
                        for sid in d.get("done") or sids:
                            self.absences.setdefault(real_lid, {})[sid] = 1
                if lid:
                    self.load_lesson(lid)

            def err(e, keys=keys):
                self.in_flight -= keys
                self.set_result(f"⚠ {title}: {e}", err=True)

            self.run(fn, ok, err)
        self.render()

    def delete(self, kind, ids, lid, title):
        key = f"del-{kind}-{','.join(map(str, ids))}"
        if key in self.in_flight or not self.mesh:
            return
        self.in_flight.add(key)
        self.set_result(f"удаляю {title}…")
        mesh, remote = self.mesh, self.remote

        def ok(d):
            self.in_flight.discard(key)
            if d.get("error"):
                self.set_result(f"⚠ удаление {title}: {d['error']}", err=True)
            else:
                self.set_result(f"✕ удалено: {title}")
                self.history = [h for h in self.history if not set(h["ids"]) & set(ids)]
            self.load_lesson(lid)

        def err(e):
            self.in_flight.discard(key)
            self.set_result(f"⚠ удаление {title}: {e}", err=True)

        self.run(lambda: remote.delete_data(mesh, kind, ids), ok, err)
        self.render()

    def do_undo(self):
        if not self.history:
            self.set_result("Нечего отменять: пульт сегодня ещё ничего не ставил")
            return
        h = self.history.pop()
        self.delete(h["kind"], h["ids"], h["lesson_id"], h["title"])

    def do_nohw(self):
        self.perform("nohomework", [], None, [], "без д/з (текущий урок)")

    def roll_start(self, key):
        self.roll[key] = set()
        self.render()

    def roll_toggle(self, key, sid):
        self.roll[key] ^= {sid}
        self.render()

    def roll_cancel(self, key):
        self.roll.pop(key, None)
        self.render()

    def roll_submit(self, key, lids):
        ids = sorted(self.roll.get(key) or [])
        if not ids:
            self.set_result("Никто не отмечен — отметь отсутствующих кликом по фамилии")
            return
        self.roll.pop(key, None)
        where = f" на {len(lids)} урока" if len(lids) > 1 else ""
        self.perform("absence", ids, None, lids, f"перекличка: {len(ids)} уч.{where}")


# ---------- самопроверка (CI) ----------

def selftest(hold_ms=0):
    """Окно на тестовых данных: импорт модулей, отрисовка всех режимов, без сети."""
    import mesh_automation_v3  # noqa: F401  — в сборке должны быть оба модуля и их зависимости
    import mesh_remote  # noqa: F401
    import requests  # noqa: F401
    root = tk.Tk()
    app = PultApp(root, offline=True)
    app.lessons = [
        {"group": "7А", "group_id": 1, "subject": "Труд", "begin": "08:30", "end": "09:15",
         "schedule_lesson_id": 11, "is_current": False},
        {"group": "8Б", "group_id": 2, "subject": "Труд", "begin": "09:30", "end": "10:15",
         "schedule_lesson_id": 21, "is_current": True},
        {"group": "8Б", "group_id": 2, "subject": "Труд", "begin": "10:30", "end": "11:15",
         "schedule_lesson_id": 22, "is_current": False},
    ]
    app.students = {1: [{"id": 101, "name": "Иванов Иван"}],
                    2: [{"id": 201, "name": "Петрова Анна"}, {"id": 202, "name": "Сидоров Пётр"},
                        {"id": 203, "name": "Кузнецова Мария"}]}
    app.grades = {21: {201: ["5"]}, 22: {202: ["4"]}}
    app.mark_ids = {21: {201: [{"id": 9001, "value": "5"}]}, 22: {202: [{"id": 9002, "value": "4"}]}}
    app.absences = {21: {203: 1}, 11: {101: 2}}
    app.absence_ids = {21: {203: 7001}, 11: {101: 7002}}
    keys = [k for k, _ in app.blocks()]
    assert len(keys) == 2, keys
    app.expanded = set(keys)
    app.render()
    app.roll[keys[1]] = {202}
    app.render()
    app.roll_cancel(keys[1])
    app.in_flight.add(app.flight_key("mark", 202, 21, 3))
    app.render()
    assert "Н 1" in app.absence_summary(app.lessons[1:]) and "УП 1" in app.absence_summary(app.lessons[:1])
    assert version_tuple("v1.2.0-cross") > version_tuple("1.1.0")
    root.update()
    if hold_ms:
        root.after(hold_ms, root.destroy)
        root.mainloop()
    else:
        root.destroy()


def main():
    if "--selftest" in sys.argv:
        # в сборке --windowed нет консоли: итог — в файл и код выхода
        hold = int(sys.argv[sys.argv.index("--hold") + 1]) if "--hold" in sys.argv else 0
        log = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else None
        try:
            selftest(hold)
            msg, code = f"SELFTEST OK {VERSION}\n", 0
        except Exception:
            import traceback
            msg, code = traceback.format_exc(), 1
        if log:
            open(log, "w", encoding="utf-8").write(msg)
        sys.stderr.write(msg)
        sys.stderr.flush()
        os._exit(code)
    if "--version" in sys.argv:
        print(VERSION)
        return
    root = tk.Tk()
    PultApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
