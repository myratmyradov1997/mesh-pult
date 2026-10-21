#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт МЭШ — кроссплатформенная версия (Windows / Linux / macOS).

Один файл. Го-открыли dot-py двойным кликом — работает.
Автоматически берёт токен из cookies Chrome/Edge/Helium/Firefox (нужно
только один раз войти в https://school.mos.ru в браузере). Профиль учителя
(Profile-Id) в первый запуск вводится один раз, дальше сохраняется.

Не требуется Microsoft Office / Node / Docker.
"""
import os
import sys
import json
import base64
import datetime
import threading
import queue
import traceback

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
C_DIM = "#9aa0aa"
C_ACCENT = "#3ecfc3"  # teal
C_PURPLE = "#a78bfa"
C_ORANGE = "#f59e0b"
C_DANGER = "#ef4444"

TOKEN_FILE = os.path.expanduser("~/.mesh_token")
REFRESH_FILE = os.path.expanduser("~/.mesh_refresh_token")
PROFILE_FILE = os.path.expanduser("~/.mesh_profile_id")


def jwt_data(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def token_valid(token):
    try:
        exp = jwt_data(token).get("exp")
        if exp and exp > datetime.datetime.now().timestamp():
            return True
    except Exception:
        pass
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


def try_get_token():
    """Цепочка: файл → refresh → браузер (без интерактивных prompt)."""
    import mesh_automation_v3 as v3
    tok = read_token()
    if tok:
        return tok, None
    # 1) refresh (если был refresh-токен в файле)
    try:
        tok = v3.refresh_mesh_token()
        if tok:
            return tok, None
    except Exception as e:
        print("refresh ошибка:", e, file=sys.stderr)
    # 2) из cookies браузера (Chrome/Edge/Helium/Firefox)
    try:
        tok = v3.get_token_from_browser()
        if tok:
            try:
                open(TOKEN_FILE, "w").write(tok)
                os.chmod(TOKEN_FILE, 0o600)
            except Exception:
                pass
            return tok, None
    except Exception as e:
        print("cookie ошибка:", e, file=sys.stderr)
    return None, "не найден токен. Откройте https://school.mos.ru и войдите в «Кабинет учителя», затем нажмите «Обновить»."


def read_profile():
    pid = os.environ.get("MESH_PROFILE_ID")
    if pid:
        return pid
    if os.path.exists(PROFILE_FILE):
        p = open(PROFILE_FILE).read().strip()
        if p:
            return p
    return None


def save_profile(pid):
    try:
        open(PROFILE_FILE, "w").write(str(pid))
        os.chmod(PROFILE_FILE, 0o600)
    except Exception:
        pass


def work():
    """Вызовы API в work-потоке (нужен отдельный thread под GUI)."""
    pass


class PultApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.lessons = []
        self.blocks = []           # список {"lessons": [...]}
        self.expanded = set()
        self.block_choice = {}     # blockKey -> индекс активного урока
        self.students_cache = {}
        self.grades_cache = {}
        self.absence_cache = {}
        self.cache_day = ""
        self.token_exp = ""
        self.mesh = None
        self.mods = None
        self.busy = False

        root.title("Пульт МЭШ")
        root.geometry("560x720")
        root.configure(bg=C_BG)
        self._build()
        self._tick()
        # автозапуск апдейта через 400 мс
        root.after(400, self.refresh_all)

    # ---------- UI ----------
    def _build(self):
        top = tk.Frame(self.root, bg=C_PANEL)
        top.pack(fill="x")
        self.status = tk.Label(top, text="Пульт МЭШ", bg=C_PANEL, fg=C_TEXT,
                               font=("Segoe UI", 12, "bold"), anchor="w")
        self.status.pack(side="left", padx=10, pady=8)
        self.btn_refresh = tk.Button(top, text="Обновить", command=self.refresh_all,
                                     bg=C_ROW, fg=C_TEXT, activebackground=C_ROW_CUR,
                                     activeforeground=C_TEXT, relief="flat", padx=10)
        self.btn_refresh.pack(side="right", padx=6, pady=8)
        self.btn_nohw = tk.Button(top, text="Без д/з сейчас", command=self.do_nohw,
                                  bg=C_ROW, fg=C_TEXT, activebackground=C_ROW_CUR,
                                  activeforeground=C_TEXT, relief="flat", padx=10)
        self.btn_nohw.pack(side="right", padx=6, pady=8)

        # скроллящаяся область
        self.canvas = tk.Canvas(self.root, bg=C_BG, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.body = tk.Frame(self.canvas, bg=C_BG)
        self.canvas.create_window((0, 0), window=self.body, anchor="nw", tags="body")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure("body", width=e.width))
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")
        self._bind_wheel()

    def _bind_wheel(self):
        def on_wheel(e):
            n = -1 if getattr(e, "delta", 0) > 0 or getattr(e, "num", 4) in (4,) else 1
            self.canvas.yview_scroll(n, "units")
        self.canvas.bind_all("<MouseWheel>", on_wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _mkrow(self, parent, text, bg, fg, on_click=None):
        f = tk.Frame(parent, bg=bg, pady=8, padx=10)
        l = tk.Label(f, text=text, bg=bg, fg=fg, anchor="w",
                     font=("Segoe UI", 10), wraplength=460, justify="left")
        l.pack(fill="x")
        if on_click:
            f.bind("<Button-1>", lambda e: on_click())
            l.bind("<Button-1>", lambda e: on_click())
            f.configure(cursor="hand2")
            l.configure(cursor="hand2")
        return f

    def _mkbtn(self, parent, text, bg, fg, cmd):
        b = tk.Button(parent, text=text, bg=bg, fg=fg, activebackground=bg,
                      activeforeground=fg, relief="flat", padx=8, pady=3,
                      font=("Segoe UI", 9, "bold"), command=cmd, cursor="hand2")
        return b

    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        if not self.lessons:
            self._mkrow(self.body, "Нет уроков сегодня.", C_ROW, C_TEXT).pack(fill="x", pady=4)
        # группировка сдвоенных уроков
        self.blocks = []
        i = 0
        while i < len(self.lessons):
            grp = self.lessons[i].get("group_id")
            j = i
            while (j + 1 < len(self.lessons)
                   and self.lessons[j + 1].get("group_id") == grp
                   and grp is not None):
                j += 1
            self.blocks.append({"lessons": [self.lessons[k] for k in range(i, j + 1)]})
            i = j + 1
        for bi, blk in enumerate(self.blocks):
            ls = blk["lessons"]
            key = f"blk{bi}"
            is_current = any(l.get("is_current") for l in ls)
            bg = C_ROW_CUR if is_current else C_ROW
            if len(ls) == 1:
                l = ls[0]
                t = ("🟢 " if is_current else "⚪️ ") + \
                    (f"{l.get('begin','?')}–{l.get('end','?')} " if l.get("begin") else "") + \
                    f"{l.get('subject','?')} · {l.get('group','?')}"
                w = self._mkrow(self.body, t, bg, C_TEXT,
                                on_click=lambda bi=bi: self.toggle(bi))
                w.pack(fill="x", padx=8, pady=3)
                self.chrome(bi)
            else:
                t = ("🟢 " if is_current else "⚪️ ") + \
                    f"{ls[0].get('begin','?')}–{ls[-1].get('end','?')} " + \
                    f"{ls[0].get('subject','?')} · {ls[0].get('group','?')} · " + \
                    f"{len(ls)} урока"
                w = self._mkrow(self.body, t, bg, C_TEXT,
                                on_click=lambda bi=bi: self.toggle(bi))
                w.pack(fill="x", padx=8, pady=3)
                self.chrome(bi)
            if bi in self.expanded:
                self.render_detail(bi, key)
        self.status.config(text=f"Уроков: {len(self.lessons)} · токен до {self.token_exp}")

    def render_detail(self, bi, key):
        blk = self.blocks[bi]
        act = self.block_choice.get(key, 0)
        wrap = tk.Frame(self.body, bg=C_BG)
        wrap.pack(fill="x", padx=16, pady=(0, 6))
        active = blk["lessons"][act]
        if len(blk["lessons"]) > 1:
            tk.Label(wrap, text="Отметки идут в:", bg=C_BG, fg=C_DIM,
                     font=("Segoe UI", 9)).pack(anchor="w")
            row = tk.Frame(wrap, bg=C_BG)
            row.pack(anchor="w", pady=2)
            for k2, l in enumerate(blk["lessons"]):
                sel = (k2 == act)
                b = self._mkbtn(row, f"{k2+1}-й ({l.get('begin','?')})",
                                C_ACCENT if sel else C_ROW, C_TEXT,
                                cmd=lambda k2=k2: self.pick_lesson(bi, key, k2))
                b.pack(side="left", padx=3)
        st = self.students_cache.get(active.get("group_id"))
        if st is None:
            tk.Label(wrap, text="Загружаю учеников…", bg=C_BG, fg=C_DIM,
                     font=("Segoe UI", 9)).pack(anchor="w")
            self.load_students(active.get("group_id"), bi)
            return
        gid = active.get("group_id")
        gl_active = self.grades_cache.get(active.get("schedule_lesson_id"), {})
        for s in st:
            row = tk.Frame(wrap, bg=C_BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=s["name"], bg=C_BG, fg=C_TEXT,
                     font=("Segoe UI", 9), width=24, anchor="w").pack(side="left")
            for g in [5, 4, 3, 2]:
                set_active = str(g) in (gl_active.get(s["id"]) or [])
                set_other = any(
                    str(g) in (self.grades_cache.get(o["schedule_lesson_id"], {}).get(s["id"]) or [])
                    for o in blk["lessons"] if o is not active)
                bg = C_ACCENT if set_active else (C_PURPLE if set_other else C_ROW)
                b = self._mkbtn(row, str(g), bg, C_TEXT,
                                cmd=lambda g=g, s=s: self.do_mark(bi, key, s, g))
                b.pack(side="left", padx=2)
            set_h = (s["id"] in self.absence_cache.get(active.get("schedule_lesson_id") or 0, set()))
            nb = self._mkbtn(row, "Н", C_ORANGE if set_h else C_ROW,
                             C_TEXT, cmd=lambda s=s: self.do_absence(bi, key, s))
            nb.pack(side="left", padx=2)

    def chrome(self, bi):
        """Держим пустое место под деталями Выделенного урока."""
        pass

    # ---------- Логика ----------
    def pick_lesson(self, bi, key, idx):
        self.block_choice[key] = idx
        self.render()

    def toggle(self, bi):
        key = f"blk{bi}"
        if key in self.expanded:
            self.expanded.remove(key)
        else:
            blk = self.blocks[bi]
            idx = next((i for i, l in enumerate(blk["lessons"]) if l.get("is_current")),
                       len(blk["lessons"]) - 1)
            self.block_choice[key] = idx
            self.expanded.add(key)
            # грузим оценки всех уроков блока
            for l in blk["lessons"]:
                sid = l.get("schedule_lesson_id")
                if sid and sid not in self.grades_cache:
                    self.fetch_grades(sid, bi)
        self.render()

    def _worker(self, fn, after):
        t = threading.Thread(target=lambda: self.q.put(("result", fn(), after)), daemon=True)
        t.start()

    def _pump(self):
        while True:
            try:
                kind, data, after = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                self.fail(data)
            else:
                after(data)

    def load_students(self, gid, bi):
        def do():
            return self.mods.get_student_profiles(gid)
        threading.Thread(target=lambda: self._thread_students(gid), daemon=True).start()

    def _thread_students(self, gid):
        try:
            st = self.mods.get_student_profiles(gid)
            self.q.put(("result", gid, st))
        except Exception as e:
            self.q.put(("error", f"Ученики: {e}", None))

    def fetch_grades(self, sid, bi):
        threading.Thread(target=lambda: self._thread_grades(sid), daemon=True).start()

    def _thread_grades(self, sid):
        try:
            ms = self.mods.get_marks(sid)
            by = {}
            for x in ms or []:
                if not isinstance(x, dict) or x.get("deleted_by") is not None:
                    continue
                spid = x.get("student_profile_id")
                if not spid:
                    continue
                name = str(x.get("name") or "?")
                by.setdefault(spid, []).append(name)
            self.q.put(("result", sid, by))
        except Exception as e:
            self.q.put(("error", f"Оценки: {e}", None))

    def refresh_all(self):
        self.status.config(text="Загрузка…")
        threading.Thread(target=self._thread_full, daemon=True).start()

    def _thread_full(self):
        try:
            import mesh_automation_v3 as v3
            tok, err = try_get_token()
            if not tok:
                self.q.put(("error", err, None)); return
            pid = read_profile()
            if not pid:
                self.q.put(("need_profile", None, None)); return
            self.mods = v3.MeshAutomation(bearer_token=tok, profile_id=str(pid))
            ls = self.mods.get_daily_schedule()
            today = datetime.date.today().strftime("%Y-%m-%d")
            if self.cache_day != today:
                self.cache_day = today
                self.students_cache.clear()
                self.grades_cache.clear()
                self.absence_cache.clear()
                self.expanded.clear()
            self.lessons = []
            for l in ls:
                begin = l.get("time_start") or l.get("begin_time")
                end = l.get("time_end") or l.get("end_time")
                cur = False
                try:
                    b = datetime.datetime.strptime(begin, "%H:%M")
                    e2 = datetime.datetime.strptime(end or begin, "%H:%M")
                    now = datetime.datetime.now()
                    cur = b <= now <= e2
                except Exception:
                    pass
                self.lessons.append({
                    "begin": begin, "end": end, "cur": cur,
                    "subject": l.get("subject_name") or "?",
                    "group": l.get("group_name") or l.get("class_unit_name") or "?",
                    "group_id": l.get("group_id"),
                    "schedule_lesson_id": l.get("schedule_lesson_id"),
                    "is_current": cur,
                })
            exp = ""
            try:
                exp = datetime.datetime.fromtimestamp(
                    jwt_data(tok)["exp"]).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
            self.q.put(("result", None, exp))
        except Exception as e:
            self.q.put(("error", str(e), None))

    def _pump(self):
        while True:
            try:
                kind, data, extra = self.q.get_nowait()
            except queue.Empty:
                return
            if kind == "result":
                if extra and isinstance(extra, str) and "токен" not in extra:
                    self.token_exp = extra
                if kind == "result":
                    self.token_exp = extra if extra else self.token_exp
                self._tick()
            elif kind == "need_profile":
                self.status.config(text="Нужен Profile-Id (один раз)")
                self.ask_profile()
            elif kind == "need_token":
                self.status.config(text="Войдите в school.mos.ru и нажмите Обновить")
            elif kind == "error":
                self.fail(data)

    def _thread_full2(self):
        self._thread_full()

    def _tick(self):
        self.render()
        self.root.after(200, self._pump)

    def ask_profile(self):
        d = messagebox.askyesno(
            "Профиль учителя",
            "Откройте school.mos.ru → «Журнал» → F12 → вкладка Network →\n"
            "выберите любой запрос к api → в заголовках найдите Profile-Id.\n"
            "Это число нужно скопировать и вставить один раз.\n\n"
            "Вставить сейчас?")
        if not d:
            return
        import tkinter.simpledialog as sd
        pid = sd.askstring("Profile-Id", "Ваш Profile-Id (только число):")
        if pid:
            save_profile(pid.id if hasattr(pid, "id") else pid)
            self.refresh_all()

    def do_mark(self, bi, key, student, grade):
        blk = self.blocks[bi]
        act = blk["lessons"][self.block_choice.get(key, 0)]
        sid = act.get("schedule_lesson_id")
        st_id = student["id"]
        threading.Thread(target=self._thread_mark, args=(sid, st_id, grade), daemon=True).start()

    def _thread_mark(self, sid, st_id, grade):
        try:
            m = self.mods
            lesson = next(l for l in m.get_daily_schedule()
                          if l.get("schedule_lesson_id") == sid)
            cfid, tid = m.get_lesson_params(m, lesson)
            m.add_mark(schedule_lesson_id=sid, student_profile_id=st_id,
                       teacher_id=int(m.profile_id), grade=int(grade),
                       control_form_id=cfid, theme_frame_integration_id=tid)
            self.q.put(("result", sid, {"grade": grade, "student": st_id}))
        except Exception as e:
            self.q.put(("error", f"Оценка: {e}", None))

    def do_absence(self, bi, key, student):
        blk = self.blocks[bi]
        act = blk["lessons"][self.block_choice.get(key, 0)]
        sid = act.get("schedule_lesson_id")
        threading.Thread(target=self._thread_absence, args=(sid, student["id"]), daemon=True).start()

    def _thread_absence(self, sid, st_id):
        try:
            m = self.mods
            m.add_absence(
                schedule_lesson_id=sid,
                student_profile_id=st_id,
                teacher_id=int(m.profile_id),
                absence_reason_id=1,   # 1 = Н (неуважительная)
            )
            self.q.put(("result", sid, {"absence": st_id}))
        except Exception as e:
            self.q.put(("error", f"Н: {e}", None))

    def do_nohw(self):
        threading.Thread(target=self._thread_nohw, daemon=True).start()

    def _thread_nohw(self):
        try:
            m = self.mods
            lessons = m.get_daily_schedule()
            cur = m.find_current_lesson(lessons)
            grp = cur.get("group_id")
            if m.homework_absence_exists(grp, datetime.datetime.now().strftime("%d.%m.%Y")):
                self.q.put(("result", None, "Без д/з уже стоит"))
            else:
                m.add_homework_absence(grp, datetime.datetime.now())
                self.q.put(("result", None, "Без д/з ✓"))
        except Exception as e:
            self.q.put(("error", f"Без д/з: {e}", None))

    def fail(self, msg):
        messagebox.showerror("Ошибка", msg)
        self.status.config(text="Ошибка")


def main():
    root = tk.Tk()
    app = PultApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
