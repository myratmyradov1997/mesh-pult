#!/usr/bin/env python3
"""mesh_remote.py — CLI-мост «учительского пульта» к mesh_automation_v3.

Команды (результат — JSON на stdout, весь шум — в stderr):
  today                      — уроки учителя на сегодня (+ is_current)
  students --group-id N      — ученики группы
  status                     — токен (до когда), профиль
  act mark --student-id N --grade 5 [--dry]      — оценка на ТЕКУЩЕМ уроке
  act absence --student-id N [--dry]             — Н на текущем уроке
  act nohomework [--dry]                          — «без д/з» на текущем уроке

Всё переиспользует mesh_automation_v3 (единый источник: токен-цепочка,
параметры урока, API).
"""
import os
import sys
import json
import base64
import argparse
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def out(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False), file=REAL_OUT)
    REAL_OUT.flush()
    return 0


def die(msg: str) -> int:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=REAL_OUT)
    REAL_OUT.flush()
    return 1


# «настоящий» stdout: всё, что печатают библиотеки, уйдёт в stderr
REAL_OUT = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")


def fresh_mesh():
    """Экземпляр MeshAutomation с рабочим токеном и профилем."""
    import mesh_automation_v3 as m
    token = m.get_token(force_refresh=False)
    if not token:
        raise RuntimeError("нет токена — нужен вход в school.mos.ru в Helium")
    profile = m.get_profile_id()
    if not profile:
        raise RuntimeError("нет профиля (~/.mesh_profile_id)")
    return m, m.MeshAutomation(bearer_token=token, profile_id=profile)


def lesson_public(l: dict) -> dict:
    from datetime import datetime, timedelta
    # v3 отдаёт time_start/time_end (может быть begin_time из «сырых» данных)
    begin_s = l.get("time_start") or l.get("begin_time")
    end_s = l.get("time_end") or l.get("end_time")
    cur = False
    try:
        begin = datetime.strptime(begin_s, "%H:%M")
        end = datetime.strptime(end_s or begin_s, "%H:%M")
        if end < begin:
            end = end + timedelta(days=1)
        now = datetime.now()
        cur = begin <= now <= end
    except Exception:
        pass
    return {
        "group": l.get("group_name") or l.get("class_unit_name") or "?",
        "group_id": l.get("group_id"),
        "subject": l.get("subject_name") or "?",
        "subject_id": l.get("subject_id"),
        "begin": begin_s,
        "end": end_s,
        "lesson_number": l.get("lesson_number"),
        "schedule_lesson_id": l.get("schedule_lesson_id"),
        "theme_id": l.get("theme_id"),
        "is_current": cur,
    }


def cmd_today() -> int:
    m, mesh = fresh_mesh()
    lessons = mesh.get_daily_schedule()
    return out({"lessons": [lesson_public(l) for l in lessons]})


def cmd_students(group_id: int) -> int:
    m, mesh = fresh_mesh()
    profiles = mesh.get_student_profiles(group_id)
    students = [
        {"id": p.get("id"),
         "name": p.get("short_name") or p.get("full_name") or p.get("user_name") or "?"}
        for p in profiles
    ]
    students.sort(key=lambda s: s["name"])
    return out({"students": students})


def cmd_status() -> int:
    from datetime import datetime
    m, mesh = fresh_mesh()
    tok = (mesh.session.headers.get("Authorization") or "").replace("Bearer ", "")
    exp = None
    try:
        payload = tok.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data.get("exp"):
            exp = datetime.fromtimestamp(data["exp"]).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return out({"profile_id": mesh.profile_id, "token_expires": exp})


def cmd_grades(schedule_lesson_id=None) -> int:
    m, mesh = fresh_mesh()
    lesson = resolve_current(mesh, schedule_lesson_id)
    marks = mesh.get_marks(lesson["schedule_lesson_id"])
    by_student: dict = {}
    for x in marks:
        if not isinstance(x, dict):
            continue
        if x.get("deleted_by") is not None:
            continue
        sid = x.get("student_profile_id")
        if not sid:
            continue
        name = str(x.get("name") or "?")
        by_student.setdefault(sid, []).append(name)
    return out({"lesson": lesson_public(lesson), "grades": by_student})


def resolve_current(mesh, schedule_lesson_id=None, group_id=None):
    lessons = mesh.get_daily_schedule()
    if schedule_lesson_id:
        for l in lessons:
            if l.get("schedule_lesson_id") == schedule_lesson_id:
                return l
        raise RuntimeError(f"урок {schedule_lesson_id} не найден в расписании на сегодня")
    cur = mesh.find_current_lesson(lessons)
    if not cur:
        raise RuntimeError("сейчас нет урока по расписанию")
    return cur


def cmd_act(kind: str, student_id=None, grade=None, dry: bool = False,
            schedule_lesson_id=None, group_id=None) -> int:
    from datetime import datetime
    m, mesh = fresh_mesh()
    cur_lesson = resolve_current(mesh, schedule_lesson_id, group_id)
    control_form_id, theme_id = m.get_lesson_params(mesh, cur_lesson)
    base = {
        "lesson": lesson_public(cur_lesson),
        "control_form_id": control_form_id,
        "theme_frame_integration_id": theme_id,
    }
    if dry:
        base["dry"] = True
        return out(base)
    if control_form_id is None or theme_id is None:
        base["error"] = "параметры урока не определены (форма контроля/тема)"
        return out(base)
    if kind == "mark":
        if grade is None or student_id is None:
            base["error"] = "нужны --student-id и --grade"
            return out(base)
        with redirect_stdout(sys.stderr):
            mesh.add_mark(
                schedule_lesson_id=cur_lesson["schedule_lesson_id"],
                student_profile_id=student_id,
                teacher_id=int(mesh.profile_id),
                grade=int(grade),
                control_form_id=control_form_id,
                theme_frame_integration_id=theme_id,
            )
        base["result"] = "ok"
        return out(base)
    if kind == "absence":
        if student_id is None:
            base["error"] = "нужен --student-id"
            return out(base)
        with redirect_stdout(sys.stderr):
            mesh.add_absence(
                schedule_lesson_id=cur_lesson["schedule_lesson_id"],
                student_profile_id=student_id,
                teacher_id=int(mesh.profile_id),
            )
        base["result"] = "ok"
        return out(base)
    if kind == "nohomework":
        grp = cur_lesson.get("group_id")
        if not grp:
            base["error"] = "не найден group_id текущего урока"
            return out(base)
        with redirect_stdout(sys.stderr):
            if mesh.homework_absence_exists(grp, datetime.now().strftime("%d.%m.%Y")):
                base["result"] = "already"
                return out(base)
            mesh.add_homework_absence(grp, datetime.now())
        base["result"] = "ok"
        return out(base)
    base["error"] = f"неизвестное действие {kind}"
    return out(base)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["today", "students", "status", "act", "grades"])
    args, rest = ap.parse_known_args()

    # шум библиотек (get_token, api-логи v3) — в stderr; JSON — только в настоящий stdout
    sys.stdout = sys.stderr

    try:
        if args.command == "today":
            return cmd_today()
        if args.command == "grades":
            p = argparse.ArgumentParser()
            p.add_argument("--schedule-lesson-id", type=int)
            a = p.parse_args(rest)
            return cmd_grades(a.schedule_lesson_id)
        if args.command == "students":
            p = argparse.ArgumentParser()
            p.add_argument("--group-id", type=int, required=True)
            a = p.parse_args(rest)
            return cmd_students(a.group_id)
        if args.command == "status":
            return cmd_status()
        p = argparse.ArgumentParser()
        p.add_argument("kind", choices=["mark", "absence", "nohomework"])
        p.add_argument("--student-id", type=int)
        p.add_argument("--grade", type=int)
        p.add_argument("--dry", action="store_true")
        p.add_argument("--schedule-lesson-id", type=int)
        p.add_argument("--group-id", type=int)
        a = p.parse_args(rest)
        return cmd_act(a.kind, a.student_id, a.grade, a.dry, a.schedule_lesson_id, a.group_id)
    except RuntimeError as e:
        return die(str(e))
    except SystemExit as e:  # RuntimeError с сообщением внутри v3 бывают и тут
        code = e.code if e.code is None else e.code
        if code not in (None, 0):
            return die(str(code) if not isinstance(code, int) else f"код выхода {code}")
        return 0
    except Exception as e:
        return die(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
