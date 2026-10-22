#!/usr/bin/env python3
"""mesh_remote.py — CLI-мост «учительского пульта» к mesh_automation_v3.

Команды (результат — JSON на stdout, весь шум — в stderr):
  today                      — уроки учителя на сегодня (+ is_current)
  students --group-id N      — ученики группы
  status                     — токен (до когда), профиль
  grades [--schedule-lesson-id N]                — оценки и Н урока (с id записей)
  act mark --student-id N --grade 5 [--dry]      — оценка на ТЕКУЩЕМ уроке
  act absence --student-id N [--dry]             — Н на текущем уроке
  act absence --student-ids 1,2,3                 — Н нескольким ученикам (перекличка)
  act nohomework [--dry]                          — «без д/з» на текущем уроке
  delete mark --ids 1,2                           — удалить оценки по id
  delete absence --ids 1,2                        — удалить Н по id

Всё переиспользует mesh_automation_v3 (единый источник: токен-цепочка,
параметры урока, API). Функции *_data(...) возвращают dict и годятся для
прямого импорта (кроссплатформенный GUI); cmd_* — CLI-обёртки над ними.
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
REAL_OUT = sys.stdout


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
        # strptime даёт 1900-01-01 — переносим время урока на сегодня, иначе now всегда позже конца
        now = datetime.now()
        begin = datetime.combine(now.date(), datetime.strptime(begin_s, "%H:%M").time())
        end = datetime.combine(now.date(), datetime.strptime(end_s or begin_s, "%H:%M").time())
        if end < begin:
            end = end + timedelta(days=1)
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


# ---------- данные (dict) ----------

def today_data(mesh) -> dict:
    return {"lessons": [lesson_public(l) for l in mesh.get_daily_schedule()]}


def students_data(mesh, group_id: int) -> dict:
    profiles = mesh.get_student_profiles(group_id)
    students = [
        {"id": p.get("id"),
         "name": p.get("short_name") or p.get("full_name") or p.get("user_name") or "?"}
        for p in profiles
    ]
    students.sort(key=lambda s: s["name"])
    return {"students": students}


def token_expires(mesh):
    from datetime import datetime
    tok = (mesh.session.headers.get("Authorization") or "").replace("Bearer ", "")
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data.get("exp"):
            return datetime.fromtimestamp(data["exp"]).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return None


def status_data(mesh) -> dict:
    return {"profile_id": mesh.profile_id, "token_expires": token_expires(mesh)}


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


def lesson_marks(mesh, schedule_lesson_id: int) -> dict:
    """student_id → [{"id", "value"}] по порядку выставления (удалённые отброшены)."""
    by_student: dict = {}
    for x in mesh.get_marks(schedule_lesson_id):
        if not isinstance(x, dict) or x.get("deleted_by") is not None:
            continue
        sid = x.get("student_profile_id")
        if not sid:
            continue
        by_student.setdefault(sid, []).append({"id": x.get("id"), "value": str(x.get("name") or "?")})
    for v in by_student.values():
        v.sort(key=lambda r: r["id"] or 0)
    return by_student


def lesson_absences(mesh, schedule_lesson_id: int) -> dict:
    """student_id → {"id", "reason"} (reason: 1 = Н, 2 = УП)."""
    res: dict = {}
    for a in mesh.get_attendances(schedule_lesson_id):
        if isinstance(a, dict) and a.get("student_profile_id"):
            res[a["student_profile_id"]] = {"id": a.get("id"), "reason": a.get("absence_reason_id") or 1}
    return res


def grades_data(mesh, schedule_lesson_id=None) -> dict:
    lesson = resolve_current(mesh, schedule_lesson_id)
    lid = lesson["schedule_lesson_id"]
    marks = lesson_marks(mesh, lid)
    data = {
        "lesson": lesson_public(lesson),
        "grades": {sid: [r["value"] for r in rs] for sid, rs in marks.items()},
        "mark_ids": marks,
        "absences": {},
        "absence_ids": {},
    }
    # ошибка чтения отсутствий не должна ломать оценки
    try:
        ab = lesson_absences(mesh, lid)
        data["absences"] = {sid: a["reason"] for sid, a in ab.items()}
        data["absence_ids"] = {sid: a["id"] for sid, a in ab.items()}
    except Exception as e:
        print(f"attendances: {e}", file=sys.stderr)
    return data


def day_data(mesh) -> dict:
    """Всё для панели одним процессом: расписание, срок токена, оценки и Н всех уроков дня.
    lessons_data: schedule_lesson_id → {grades, mark_ids, absences, absence_ids}."""
    lessons = mesh.get_daily_schedule()
    lids = [l["schedule_lesson_id"] for l in lessons if l.get("schedule_lesson_id")]
    per = {lid: {"grades": {}, "mark_ids": {}, "absences": {}, "absence_ids": {}} for lid in lids}
    for lid in lids:
        try:
            marks = lesson_marks(mesh, lid)
            per[lid]["grades"] = {sid: [r["value"] for r in rs] for sid, rs in marks.items()}
            per[lid]["mark_ids"] = marks
        except Exception as e:
            print(f"marks {lid}: {e}", file=sys.stderr)
    # Н/УП всех уроков дня — одним запросом
    try:
        for a in (mesh.get_attendances(lids) if lids else []):
            lid, sid = a.get("schedule_lesson_id"), a.get("student_profile_id")
            if isinstance(a, dict) and lid in per and sid:
                per[lid]["absences"][sid] = a.get("absence_reason_id") or 1
                per[lid]["absence_ids"][sid] = a.get("id")
    except Exception as e:
        print(f"attendances: {e}", file=sys.stderr)
    return {
        "lessons": [lesson_public(l) for l in lessons],
        "token_expires": token_expires(mesh),
        "lessons_data": per,
        "sick": sick_today(mesh, lessons),
    }


def sick_today(mesh, lessons) -> dict:
    """Болеющие сегодня по ЕМИАС (в журнале значок «б», Н при этом не стоит).
    group_id → student_id → SICK | SICK_WITH_INFECTION."""
    from datetime import date
    day = date.today().isoformat()
    by_unit: dict = {}   # class_unit_id → записи ЕМИАС (один класс бывает в нескольких группах)
    sick: dict = {}
    for gid in {l.get("group_id") for l in lessons if l.get("group_id")}:
        try:
            profiles = mesh.get_student_profiles(gid)
            members = {p.get("id") for p in profiles}
            for cu in {(p.get("class_unit") or {}).get("id") for p in profiles} - {None}:
                if cu not in by_unit:
                    by_unit[cu] = mesh.get_medical_recommendations(cu, day)
                for rec in by_unit[cu]:
                    sid = rec.get("student_profile_id")
                    if rec.get("type") in ("SICK", "SICK_WITH_INFECTION") and sid in members:
                        sick.setdefault(gid, {})[sid] = rec["type"]
        except Exception as e:
            print(f"emias {gid}: {e}", file=sys.stderr)
    return sick


def act_data(m, mesh, kind: str, student_id=None, grade=None, dry: bool = False,
             schedule_lesson_id=None, group_id=None, student_ids=None) -> dict:
    """Записать в журнал. В ответе created: student_id → id новой записи (для удаления)."""
    from datetime import datetime
    cur_lesson = resolve_current(mesh, schedule_lesson_id, group_id)
    lid = cur_lesson["schedule_lesson_id"]
    with redirect_stdout(sys.stderr):
        control_form_id, theme_id = m.get_lesson_params(mesh, cur_lesson)
    base = {
        "lesson": lesson_public(cur_lesson),
        "control_form_id": control_form_id,
        "theme_frame_integration_id": theme_id,
    }
    if dry:
        base["dry"] = True
        return base
    if control_form_id is None or theme_id is None:
        base["error"] = "параметры урока не определены (форма контроля/тема)"
        return base
    if kind == "mark":
        if grade is None or student_id is None:
            base["error"] = "нужны --student-id и --grade"
            return base
        with redirect_stdout(sys.stderr):
            resp = mesh.add_mark(
                schedule_lesson_id=lid,
                student_profile_id=student_id,
                teacher_id=int(mesh.profile_id),
                grade=int(grade),
                control_form_id=control_form_id,
                theme_frame_integration_id=theme_id,
            )
        mid = resp.get("id") if isinstance(resp, dict) else None
        if not mid:
            # ответ без id — берём самую свежую такую оценку ученика на уроке
            same = [r for r in lesson_marks(mesh, lid).get(student_id, []) if r["value"] == str(grade)]
            mid = same[-1]["id"] if same else None
        base["created"] = {student_id: mid}
        base["result"] = "ok"
        return base
    if kind == "absence":
        ids = list(student_ids or []) or ([student_id] if student_id is not None else [])
        if not ids:
            base["error"] = "нужен --student-id или --student-ids"
            return base
        done, failed, created = [], {}, {}
        with redirect_stdout(sys.stderr):
            for sid in ids:
                try:
                    resp = mesh.add_absence(
                        schedule_lesson_id=lid,
                        student_profile_id=sid,
                        teacher_id=int(mesh.profile_id),
                    )
                    done.append(sid)
                    if isinstance(resp, dict) and resp.get("id"):
                        created[sid] = resp["id"]
                except Exception as e:
                    failed[sid] = str(e)[:160]
        if len(created) < len(done):
            try:
                ab = lesson_absences(mesh, lid)
                for sid in done:
                    if sid not in created and sid in ab:
                        created[sid] = ab[sid]["id"]
            except Exception as e:
                print(f"attendances: {e}", file=sys.stderr)
        base["done"] = done
        base["created"] = created
        if failed:
            base["failed"] = failed
            if not done:
                base["error"] = f"Н не выставлены: {next(iter(failed.values()))}"
                return base
        base["result"] = "ok" if not failed else "partial"
        return base
    if kind == "nohomework":
        grp = cur_lesson.get("group_id")
        if not grp:
            base["error"] = "не найден group_id текущего урока"
            return base
        with redirect_stdout(sys.stderr):
            if mesh.homework_absence_exists(grp, datetime.now().strftime("%d.%m.%Y")):
                base["result"] = "already"
                return base
            mesh.add_homework_absence(grp, datetime.now())
        base["result"] = "ok"
        return base
    base["error"] = f"неизвестное действие {kind}"
    return base


def delete_data(mesh, kind: str, ids) -> dict:
    """Удалить оценки (kind=mark) или Н (kind=absence) по id записей."""
    fn = {"mark": mesh.delete_mark, "absence": mesh.delete_attendance}.get(kind)
    if fn is None:
        return {"error": f"неизвестный вид удаления {kind}"}
    deleted, failed = [], {}
    with redirect_stdout(sys.stderr):
        for i in ids:
            try:
                fn(int(i))
                deleted.append(int(i))
            except Exception as e:
                failed[int(i)] = str(e)[:160]
    res = {"deleted": deleted}
    if failed:
        res["failed"] = failed
        if not deleted:
            res["error"] = f"не удалено: {next(iter(failed.values()))}"
    return res


# ---------- CLI ----------

def _ids(v: str):
    return [int(x) for x in v.split(",") if x.strip()]


def main() -> int:
    global REAL_OUT
    # «настоящий» stdout для JSON; шум библиотек (get_token, api-логи v3) — в stderr
    REAL_OUT = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr

    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["today", "day", "students", "status", "act", "grades", "delete"])
    args, rest = ap.parse_known_args()

    try:
        if args.command == "grades":
            p = argparse.ArgumentParser()
            p.add_argument("--schedule-lesson-id", type=int)
            a = p.parse_args(rest)
        elif args.command == "students":
            p = argparse.ArgumentParser()
            p.add_argument("--group-id", type=int, required=True)
            a = p.parse_args(rest)
        elif args.command == "delete":
            p = argparse.ArgumentParser()
            p.add_argument("kind", choices=["mark", "absence"])
            p.add_argument("--ids", type=_ids, required=True)
            a = p.parse_args(rest)
        elif args.command == "act":
            p = argparse.ArgumentParser()
            p.add_argument("kind", choices=["mark", "absence", "nohomework"])
            p.add_argument("--student-id", type=int)
            p.add_argument("--student-ids", type=_ids)
            p.add_argument("--grade", type=int)
            p.add_argument("--dry", action="store_true")
            p.add_argument("--schedule-lesson-id", type=int)
            p.add_argument("--group-id", type=int)
            a = p.parse_args(rest)

        m, mesh = fresh_mesh()
        if args.command == "today":
            return out(today_data(mesh))
        if args.command == "day":
            return out(day_data(mesh))
        if args.command == "status":
            return out(status_data(mesh))
        if args.command == "grades":
            return out(grades_data(mesh, a.schedule_lesson_id))
        if args.command == "students":
            return out(students_data(mesh, a.group_id))
        if args.command == "delete":
            return out(delete_data(mesh, a.kind, a.ids))
        return out(act_data(m, mesh, a.kind, a.student_id, a.grade, a.dry,
                            a.schedule_lesson_id, a.group_id, a.student_ids))
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
