#!/usr/bin/env python3
"""
MESH Grade Automation Script V2

Этот скрипт автоматизирует выставление оценок в МЭШ (Московская электронная школа).

Новые функции V2:
- Автоматический выбор текущего урока по времени
- Пакетное выставление оценок нескольким ученикам
- Пакетная отметка отсутствия (Н)

Использование:
    python mesh_automation_v2.py

Автор: На основе записи действий пользователя
"""

import requests
import json
import re
import os
import sqlite3
import shutil
import tempfile
import time as _time  # модуль time (в файле есть from datetime import time — иначе перекрывается)
import calendar
from datetime import datetime, date, time
from typing import Optional, Dict, List, Any, Tuple

# ========== КОНФИГУРАЦИЯ ==========

# Базовый URL для API МЭШ
BASE_URL = "https://school.mos.ru"

# Маппинг номеров уроков (из API) -> время начала
# API использует нумерацию: 1, 3, 5, 7, 9, 11, 13 для 7 уроков
LESSON_NUMBER_TO_TIME = {
    1: time(8, 30),    # 1 урок (08:30-09:15)
    3: time(9, 30),    # 2 урок (09:30-10:15)
    5: time(10, 30),   # 3 урок (10:30-11:15)
    7: time(11, 30),   # 4 урок (11:30-12:15)
    9: time(12, 35),   # 5 урок (12:35-13:20)
    11: time(13, 40),  # 6 урок (13:40-14:25)
    13: time(14, 40),  # 7 урок (14:40-15:25)
}

# Обратный маппинг: порядковый номер урока (1-7) -> время
LESSON_TIMES = [
    time(8, 30),   # 1 урок
    time(9, 30),   # 2 урок
    time(10, 30),  # 3 урок
    time(11, 30),  # 4 урок
    time(12, 35),  # 5 урок
    time(13, 40),  # 6 урок
    time(14, 40),  # 7 урок
]

# Продолжительность урока в минутах
LESSON_DURATION_MINUTES = 45

# Заголовки по умолчанию (скопированы из записи)
DEFAULT_HEADERS = {
    "sec-ch-ua-platform": '"macOS"',
    "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "x-mes-hostid": "9",
    "aid": "14",  # academic_year_id (2026/2027)
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "x-mes-subsystem": "journalw",
    "Accept": "*/*",
    "Content-Type": "application/json"
}


def _hw_date_iso(value) -> Optional[str]:
    """Привести дату урока к ISO (YYYY-MM-DD); None если распознать не удалось."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-":              # 2026-12-02 / 2026-12-02T13:40:00
        return s[:10]
    parts = s.split(".")                          # 02.12.2026
    if len(parts) == 3 and all(parts):
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return None


def _hw_date_ru(value) -> Optional[str]:
    """Привести дату урока к формату МЭШ DD.MM.YYYY (строки и date/datetime)."""
    iso = _hw_date_iso(value)
    if not iso:
        return None
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def get_current_lesson_number() -> Optional[int]:
    """
    Определить номер текущего урока по времени.
    
    Returns:
        Номер урока (1-7) или None если сейчас не время уроков
    """
    now = datetime.now().time()
    
    for i, start_time in enumerate(LESSON_TIMES):
        # Вычисляем время окончания урока
        start_dt = datetime.combine(date.today(), start_time)
        end_dt = start_dt + timedelta(minutes=LESSON_DURATION_MINUTES)
        end_time = end_dt.time()
        
        # Проверяем, попадает ли текущее время в урок
        if start_time <= now <= end_time:
            return i + 1  # Уроки нумеруются с 1
    
    # Проверяем, может быть мы в перемене и скоро начнётся урок
    for i, start_time in enumerate(LESSON_TIMES):
        start_dt = datetime.combine(date.today(), start_time)
        # Если до начала урока осталось менее 10 минут
        now_dt = datetime.combine(date.today(), now)
        if timedelta(0) <= (start_dt - now_dt) <= timedelta(minutes=10):
            return i + 1
    
    return None


def parse_time_from_string(time_str: str) -> Optional[time]:
    """
    Преобразовать строку времени в объект time.
    
    Args:
        time_str: Строка времени (например "08:30", "8:30", "Урок 1", "Урок 3")
    
    Returns:
        Объект time или None
    """
    if not time_str:
        return None
    
    # Пробуем разные форматы времени
    for fmt in ["%H:%M", "%H:%M:%S", "%H.%M"]:
        try:
            return datetime.strptime(time_str, fmt).time()
        except ValueError:
            continue
    
    # Если это "Урок N", используем маппинг номеров API -> время
    match = re.search(r'урок\s*(\d+)', time_str.lower())
    if match:
        lesson_num = int(match.group(1))
        # Используем LESSON_NUMBER_TO_TIME для номеров из API (1, 3, 5, 7, 9, 11, 13)
        if lesson_num in LESSON_NUMBER_TO_TIME:
            return LESSON_NUMBER_TO_TIME[lesson_num]
    
    return None


from datetime import timedelta


class MeshAutomation:
    """Класс для автоматизации работы с МЭШ журналом."""

    def __init__(self, bearer_token: str, profile_id: str = None):
        """
        Инициализация.
        
        Args:
            bearer_token: JWT токен авторизации (из cookies)
            profile_id: ID профиля учителя (если None - загружается из конфига или запрашивается)
        """
        # Если profile_id не передан, получаем из конфига
        if profile_id is None:
            profile_id = get_profile_id()
            if not profile_id:
                raise ValueError("Profile ID не указан! Укажите его явно или через файл ~/.mesh_profile_id")
        
        self.profile_id = profile_id
        self.session = requests.Session()
        self.headers = DEFAULT_HEADERS.copy()
        self.headers["Authorization"] = f"Bearer {bearer_token}"
        self.headers["Profile-Id"] = profile_id
        self.session.headers.update(self.headers)
        
        # Кэш данных
        self._students_cache: Dict[int, List[Dict]] = {}
        self._schedule_cache: Dict[int, List[Dict]] = {}
        self._token_refresh_attempted = False
        self.last_error: Optional[str] = None

    def refresh_token(self) -> bool:
        """Попытаться обновить токен из браузера."""
        if self._token_refresh_attempted:
            print("⚠️  Токен уже был обновлён в этой сессии.")
            return False
        
        self._token_refresh_attempted = True
        print("\n🔄 Токен устарел! Пытаюсь обновить...")

        # Сначала штатная ротация (надёжнее, не зависит от браузера)
        new_token = refresh_mesh_token()
        if not new_token:
            print("🔄 refresh не сработал, пробую получить токен из браузера...")
            new_token = get_token_from_browser()
        if new_token:
            self.headers["Authorization"] = f"Bearer {new_token}"
            self.session.headers.update({"Authorization": f"Bearer {new_token}"})
            print("✅ Токен успешно обновлён!")
            
            token_file = os.path.expanduser("~/.mesh_token")
            with open(token_file, "w") as f:
                f.write(new_token)
            os.chmod(token_file, 0o600)
            return True
        
        print("❌ Не удалось автоматически получить токен из браузера.")
        return False

    # ========== API МЕТОДЫ ==========

    def get_student_profiles(self, group_id: int, class_unit_id: int = None) -> List[Dict]:
        """Получить список учеников группы."""
        if group_id in self._students_cache:
            return self._students_cache[group_id]
            
        url = f"{BASE_URL}/api/ej/core/teacher/v1/student_profiles"
        params = {
            "academic_year_id": 14,
            "group_ids": group_id,
            "with_groups": "true",
            "with_deleted": "false",
            "per_page": 150,
            "page": 1
        }
        if class_unit_id:
            params["class_unit_ids"] = class_unit_id
            
        response = self.session.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        if isinstance(data, dict):
            if 'profiles' in data:
                data = data['profiles']
            elif 'items' in data:
                data = data['items']
            elif 'data' in data:
                data = data['data']
        
        self._students_cache[group_id] = data if isinstance(data, list) else []
        return self._students_cache[group_id]

    def find_student_by_name(self, group_id: int, name: str) -> Optional[Dict]:
        """Найти ученика по имени (частичное совпадение)."""
        students = self.get_student_profiles(group_id)
        name_lower = name.lower()
        
        for student in students:
            full_name = student.get('user_name', '') or ''
            short_name = student.get('short_name', '') or ''
            combined = f"{full_name} {short_name}".lower()
            
            if name_lower in combined:
                return student
                
        return None

    def find_students_by_names(self, group_id: int, names: List[str]) -> Tuple[List[Dict], List[str]]:
        """
        Найти нескольких учеников по именам.
        
        Returns:
            (найденные ученики, ненайденные имена)
        """
        found = []
        not_found = []
        
        for name in names:
            student = self.find_student_by_name(group_id, name.strip())
            if student:
                found.append(student)
            else:
                not_found.append(name)
        
        return found, not_found

    def get_schedule_items(self, group_id: int, date_from: str = None, date_to: str = None) -> List[Dict]:
        """Получить список уроков для группы."""
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/schedule_items"
        
        if not date_from:
            today = date.today()
            date_from = today.strftime("%Y-%m-%d")
            date_to = date_from
            
        params = {
            "academic_year_id": 14,
            "group_ids": group_id,
            "from": date_from,
            "to": date_to or date_from,
            "with_group_class_subject_info": "true",
            "with_lesson_info": "true",
            "page": 1,
            "per_page": 100
        }
        
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def get_control_forms(self, subject_id: int, school_id: int = None) -> List[Dict]:
        """
        Получить формы контроля (типы оценок).
        
        Args:
            subject_id: ID предмета
            school_id: ID школы (если None - параметр не передаётся)
        """
        url = f"{BASE_URL}/api/ej/core/teacher/v1/control_forms"
        params = {
            "academic_year_id": 14,
            "subject_id": subject_id,
            "with_grade_system": "true",
            "with_deleted": "false",
            "education_level_id": 2,
            "page": 1,
            "per_page": 1000
        }
        
        if school_id:
            params["school_id"] = school_id
        
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def add_mark(
        self,
        schedule_lesson_id: int,
        student_profile_id: int,
        teacher_id: int,
        grade: int,
        control_form_id: int,
        theme_frame_integration_id: int,
        grade_system_id: int = 4140512,
        comment: str = "",
        weight: int = 1
    ) -> Dict:
        """Выставить оценку ученику."""
        url = f"{BASE_URL}/api/ej/core/teacher/v1/marks"
        
        payload = {
            "comment": comment,
            "is_exam": False,
            "is_criterion": False,
            "is_point": False,
            "point_date": "",
            "schedule_lesson_id": schedule_lesson_id,
            "student_profile_id": student_profile_id,
            "teacher_id": teacher_id,
            "control_form_id": control_form_id,
            "weight": weight,
            "theme_frame_integration_id": theme_frame_integration_id,
            "course_lesson_topic_id": None,
            "grade_origins": [
                {
                    "grade_origin": str(grade),
                    "grade_system_id": grade_system_id
                }
            ],
            "grade_system_type": False,
            "mark_type_id": 1
        }
        
        # Логирование для отладки
        print(f"   📤 API запрос: schedule_lesson_id={schedule_lesson_id}, student={student_profile_id}, grade={grade}")
        print(f"   📤 theme_frame_integration_id={theme_frame_integration_id}, control_form_id={control_form_id}")
        
        response = self.session.post(url, json=payload)
        
        # Логируем полный ответ
        print(f"   📥 API статус: {response.status_code}")
        try:
            resp_json = response.json()
            print(f"   📥 API ответ: {resp_json}")
        except:
            print(f"   📥 API ответ (raw): {response.text[:200]}")
        
        response.raise_for_status()
        return response.json()

    def get_marks(self, schedule_lesson_id: int) -> List[Dict]:
        """Оценки, выставленные за конкретный урок (GET)."""
        url = f"{BASE_URL}/api/ej/core/teacher/v1/marks"
        resp = self.session.get(url, params={"schedule_lesson_id": schedule_lesson_id})
        resp.raise_for_status()
        return resp.json()

    def add_absence(
        self,
        schedule_lesson_id: int,
        student_profile_id: int,
        teacher_id: int,
        absence_reason_id: int = 1,  # 1 = Н (неуважительная), 2 = УП (уважительная)
        comment: str = ""
    ) -> Dict:
        """
        Отметить отсутствие ученика (Н).
        
        API: POST /api/ej/core/teacher/v1/lessons/{lesson_id}/absences
        """
        url = f"{BASE_URL}/api/ej/core/teacher/v1/lessons/{schedule_lesson_id}/absences"
        
        payload = {
            "student_profile_id": student_profile_id,
            "absence_reason_id": absence_reason_id,
            "comment": comment
        }
        
        response = self.session.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    # ========== ДОМАШНИЕ ЗАДАНИЯ (homework_absences) ==========

    def get_schedule_items_short(self, group_id: int, from_date: str, per_page: int = 1000) -> List[Dict]:
        """
        Получить краткий список уроков (даты) для группы.
        
        API: GET /api/ej/plan/teacher/v1/schedule_items/short
        
        Args:
            group_id: ID группы (класса)
            from_date: Дата начала (формат YYYY-MM-DD)
            per_page: Количество записей на страницу
        
        Returns:
            Список уроков с датами
        """
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/schedule_items/short"
        params = {
            "group_id": group_id,
            "from": from_date,
            "per_page": per_page
        }
        headers = {"x-mes-subsystem": "teacherweb"}
        
        try:
            response = self.session.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get('items', []) or data.get('data', [])
            return []
        except Exception as e:
            print(f"❌ Ошибка получения schedule_items_short: {e}")
            return []

    def get_homework_absences(self, group_id: int, begin_date: str, end_date: str) -> List[Dict]:
        """
        Получить существующие записи об отсутствии домашних заданий.
        
        API: GET /api/ej/core/teacher/v1/homework_absences
        
        Args:
            group_id: ID группы
            begin_date: Дата начала (формат DD.MM.YYYY)
            end_date: Дата конца (формат DD.MM.YYYY)
        
        Returns:
            Список записей homework_absences
        """
        url = f"{BASE_URL}/api/ej/core/teacher/v1/homework_absences"
        params = {
            "group_id": group_id,
            "begin_date": begin_date,
            "end_date": end_date,
            "page": 1,
            "per_page": 100
        }
        headers = {"x-mes-subsystem": "teacherweb"}
        
        try:
            response = self.session.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get('items', []) or data.get('data', [])
            return []
        except Exception as e:
            print(f"❌ Ошибка получения homework_absences: {e}")
            return []

    def add_homework_absence(self, group_id: int, lesson_date, date_assigned_on: str = None) -> Dict:
        """
        Создать запись об отсутствии домашнего задания (отметить "без д/з").

        API: POST /api/ej/core/teacher/v1/homework_absences

        Args:
            group_id: ID группы
            lesson_date: Дата урока (DD.MM.YYYY, YYYY-MM-DD или date/datetime)
            date_assigned_on: Дата назначения (формат D.M.YYYY), по умолчанию - сегодня

        Returns:
            Ответ API. Если запись за эту дату уже существует, МЭШ отвечает 500
            ("Произошла непредвиденная ошибка") — это НЕ ошибка, а признак того,
            что "без д/з" уже проставлено; в таком случае возвращается
            {"status": "already_exists", "date": ...} и запрос не дублируется.
        """
        if not date_assigned_on:
            today = datetime.now()
            date_assigned_on = f"{today.day}.{today.month}.{today.year}"

        date_str = _hw_date_ru(lesson_date)
        if not date_str:
            raise ValueError(f"Не удалось распознать дату урока: {lesson_date!r}")

        url = f"{BASE_URL}/api/ej/core/teacher/v1/homework_absences"

        payload = {
            "group_id": group_id,
            "date": date_str,
            "created_by": int(self.profile_id),
            "date_assigned_on": date_assigned_on
        }

        headers = {"x-mes-subsystem": "teacherweb"}

        response = self.session.post(url, json=payload, headers=headers)
        if response.status_code >= 500 and self.homework_absence_exists(group_id, date_str):
            # Уникальность (group_id, date): повторная вставка = 500 у МЭШ.
            # Но 500 бывает и случайным, поэтому даём вторую попытку —
            # запись считается существующей только после подтверждения GET-ом.
            _time.sleep(1)
            response = self.session.post(url, json=payload, headers=headers)
            if response.status_code < 500:
                return response.json()
            return {"status": "already_exists", "date": date_str, "group_id": group_id}
        response.raise_for_status()
        return response.json()

    def homework_absence_exists(self, group_id: int, date_str) -> bool:
        """Проверить, есть ли уже запись "без д/з" для группы на указанную дату."""
        ru = _hw_date_ru(date_str)
        if not ru:
            return False
        iso = _hw_date_iso(date_str)
        try:
            existing = self.get_homework_absences(group_id, ru, ru)
        except Exception:
            return False
        return any(_hw_date_iso(a.get("date")) == iso for a in existing)

    # ========== КТП (Календарно-тематическое планирование) ==========

    def get_groups(self, academic_year_id: int = 14) -> List[Dict]:
        """Получить список всех групп (классов) учителя."""
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/groups"
        params = {
            "academic_year_id": academic_year_id,
            "with_periods_schedule_id": "true",
            "with_parallel_curriculum_id": "true",
            "with_teacher_loads": "true"
        }
        
        # Для API планирования нужен специфичный заголовок подсистемы
        headers = {"x-mes-subsystem": "ppktpw"}
        
        try:
            response = self.session.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ Ошибка получения групп: {e}")
            return []

    def get_calendar_plans(self, group_ids: List[int] = None, academic_year_id: int = 14) -> List[Dict]:
        """
        Получить список КТП для групп учителя.
        
        Args:
            group_ids: Список ID групп (если None, получает все)
            academic_year_id: ID академического года (по умолчанию 14 для 2026/2027)
        
        Returns:
            Список КТП
        """
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/calendar_plans"
        
        params = {
            "academic_year_id": academic_year_id
        }
        
        # Для API планирования нужен специфичный заголовок подсистемы
        headers = {"x-mes-subsystem": "ppktpw"}
        
        if group_ids:
            params["group_id"] = ",".join(str(g) for g in group_ids)
        
        try:
            response = self.session.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ Ошибка получения КТП: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   Детали: {e.response.text}")
            return []

    def get_thematic_frames(self, subject_id: int, education_level_id: int = 2) -> List[Dict]:
        """Получить тематический каркас (список тем) для предмета."""
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/thematic_frames"
        params = {
            "subject_id": subject_id,
            "education_level_id": education_level_id
        }
        # Используем стандартный заголовок подсистемы журнала, или без него.
        headers = {"x-mes-subsystem": "journalw"} # как в логе
        
        response = self.session.get(url, params=params, headers=headers)
        if response.status_code != 200:
            print(f"   ⚠️  Ошибка получения тем каркаса ({response.status_code}): {response.text[:100]}")
            return []
        
        data = response.json()
        
        # Если пусто, пробуем без education_level_id (иногда помогает)
        if not data and education_level_id is not None:
             print("   ⚠️  Список тем пуст. Пробую запрос без фильтра уровня образования...")
             params.pop("education_level_id")
             response = self.session.get(url, params=params, headers=headers)
             if response.status_code == 200:
                 data = response.json()

        return data

    def get_calendar_plan_details(self, calendar_plan_id: int) -> Optional[Dict]:
        """Получить детали конкретного КТП."""
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/calendar_plans/{calendar_plan_id}"
        headers = {"x-mes-subsystem": "ppktpw"}
        
        try:
            response = self.session.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ Ошибка получения деталей КТП {calendar_plan_id}: {e}")
            return None

    def recalc_calendar_plan(self, calendar_plan_id: int) -> bool:
        """
        Пересчитать даты КТП (кнопка "Пересчитать даты").
        
        API: POST /api/ej/plan/teacher/v1/calendar_plans/{id}/recalc?ignore_IA=true
        """
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/calendar_plans/{calendar_plan_id}/recalc?ignore_IA=true"
        headers = {"x-mes-subsystem": "ppktpw"}
        
        try:
            response = self.session.post(url, headers=headers)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"❌ Ошибка пересчёта КТП {calendar_plan_id}: {e}")
            return False

    def finish_calendar_plan(self, calendar_plan_id: int) -> bool:
        """
        Достроить даты уроков в КТП (кнопка "Достроить даты").
        
        API: POST /api/ej/plan/teacher/v1/calendar_plans/{id}/finish?ignore_IA=true
        """
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/calendar_plans/{calendar_plan_id}/finish?ignore_IA=true"
        headers = {"x-mes-subsystem": "ppktpw"}
        
        try:
            response = self.session.post(url, headers=headers)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"❌ Ошибка достройки дат КТП {calendar_plan_id}: {e}")
            return False

    def publish_calendar_plan(self, calendar_plan_id: int) -> bool:
        """
        Полный цикл обновления и публикации КТП:
        1. Пересчет дат (recalc)
        2. Достройка дат (finish)
        3. Очистка драфта
        4. Публикация (status: published)
        5. Сброс очереди test_lessons
        """
        headers = {
            "x-mes-subsystem": "ppktpw",
            "Content-Type": "application/json"
        }
        
        try:
            # 1. Recalc
            self.recalc_calendar_plan(calendar_plan_id)
            
            # 2. Finish
            self.finish_calendar_plan(calendar_plan_id)
            
            # 3. Get updated plan
            cp_url = f"{BASE_URL}/api/ej/plan/teacher/v1/calendar_plans/{calendar_plan_id}"
            r_cp = self.session.get(cp_url, headers=headers)
            r_cp.raise_for_status()
            cp_data = r_cp.json()
            
            # 4. Delete draft if exists
            draft_url = f"{BASE_URL}/api/ej/drafts/v1/{calendar_plan_id}?entity_name={calendar_plan_id}"
            try:
                self.session.delete(draft_url, headers=headers)
            except Exception:
                pass
                
            # 5. PUT with status published
            cp_data["status"] = "published"
            r_pub = self.session.put(cp_url, json=cp_data, headers=headers)
            r_pub.raise_for_status()
            
            # 6. Reset test lessons
            test_url = f"{BASE_URL}/api/ej/plan/teacher/v1/test_lessons"
            try:
                self.session.put(test_url, json=[], headers=headers)
            except Exception:
                pass
                
            print(f"✅ КТП {calendar_plan_id} успешно пересчитан, достроен и опубликован!")
            return True
        except Exception as e:
            print(f"❌ Ошибка публикации КТП {calendar_plan_id}: {e}")
            return False

    def create_lesson_plan(
        self,
        name: str,
        subject_id: int,
        class_level_id: int,
        subject_program_id: int,
        modules: List[Dict],
        week_hours: int = 1,
        study_level_id: int = 1,
        status: str = "draft",
        is_advanced: bool = False,
        book_nsi_ids: List = None,
        program_load_id: int = None
    ) -> Optional[Dict]:
        """
        Создать поурочный план (основу для КТП) в МЭШ с нуля и сохранить как черновик или опубликовать.
        
        API:
          1. POST /api/ej/plan/teacher/v1/lesson_plans
          2. PUT /api/ej/plan/teacher/v1/lesson_plans/{id}
        """
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/lesson_plans"
        headers = {
            "x-mes-subsystem": "ppktpw",
            "Content-Type": "application/json"
        }
        
        owner_id = int(self.profile_id) if self.profile_id else None
        
        payload = {
            "template_id": None,
            "owner_id": owner_id,
            "owner_ids": [owner_id] if owner_id else [],
            "name": name,
            "study_level_id": study_level_id,
            "program_load_id": program_load_id,
            "class_level_id": class_level_id,
            "subject_id": subject_id,
            "subject_program_id": subject_program_id,
            "book_nsi_ids": book_nsi_ids or [],
            "week_hours": week_hours,
            "is_advanced": is_advanced,
            "modules": modules,
            "is_template": True,
            "system": False,
            "is_adapt": False,
            "status": status
        }
        
        try:
            # Шаг 1: POST
            response = self.session.post(url, json=payload, headers=headers)
            response.raise_for_status()
            created_data = response.json()
            created_id = created_data.get('id')
            if not created_id:
                print(f"⚠️ План создан, но ID не получен в ответе: {created_data}")
                return created_data
                
            # Шаг 2: PUT для фиксации ID
            payload['id'] = created_id
            put_url = f"{BASE_URL}/api/ej/plan/teacher/v1/lesson_plans/{created_id}"
            put_res = self.session.put(put_url, json=payload, headers=headers)
            put_res.raise_for_status()
            
            print(f"✅ Поурочный план успешно создан! ID: {created_id} (статус: {status})")
            return put_res.json() if put_res.text else created_data
        except Exception as e:
            print(f"❌ Ошибка создания поурочного плана: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"   Детали ответа: {e.response.text}")
            return None

    def get_lesson_plans_for_ktp(self) -> List[Dict]:
        """Получить список поурочных планов для КТП."""
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/lesson_plans"
        params = {
            "owner_ids": self.profile_id,
            "is_template": "true",
            "status": "for_calendar_plan",
            "with_user_creator_data": "true",
            "with_groups": "true",
            "page": 1,
            "per_page": 400
        }
        headers = {"x-mes-subsystem": "ppktpw"}
        
        try:
            response = self.session.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and 'items' in data:
                return data['items']
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"❌ Ошибка получения поурочных планов: {e}")
            return []

    def get_daily_schedule(self) -> List[Dict]:
        """Получить расписание на сегодня для всех классов."""
        today = datetime.now().strftime("%Y-%m-%d")
        
        url = f"{BASE_URL}/api/ej/plan/teacher/v1/schedule_items"
        
        params = {
            "academic_year_id": 14,
            "teacher_id": int(self.headers["Profile-Id"]),
            "from": today,
            "to": today,
            "with_group_class_subject_info": "true",
            "with_lesson_info": "true",
            "with_rooms_info": "true",
            "page": 1,
            "per_page": 1000
        }
        
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 401:
                print(f"❌ Ошибка авторизации (401): токен устарел!")
                self.last_error = "Ошибка авторизации (401): токен устарел"
                if self.refresh_token():
                    print("🔄 Повторяю запрос с новым токеном...")
                    try:
                        response = self.session.get(url, params=params)
                        response.raise_for_status()
                        data = response.json()
                        self.last_error = None
                    except Exception as retry_e:
                        print(f"❌ Повторный запрос не удался: {retry_e}")
                        self.last_error = f"Ошибка повторного запроса: {retry_e}"
                        return []
                else:
                    return []
            else:
                print(f"❌ Ошибка запроса расписания: {e}")
                self.last_error = f"Ошибка запроса расписания: {e}"
                return []
        except Exception as e:
            print(f"❌ Ошибка запроса расписания: {e}")
            self.last_error = f"Ошибка запроса расписания: {e}"
            return []
        
        lessons = []
        
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if "items" in data: items = data["items"]
            elif "data" in data: items = data["data"]
            elif "activities" in data: items = data["activities"]
            
        for item in items:
            group_name = item.get("group_name", "Неизвестный класс")
            group_id = item.get("group_id")
            subject_name = item.get("subject_name", "Без предмета")
            subject_id = item.get("subject_id")
            
            begin_time = item.get("begin_time")
            end_time = item.get("end_time")
            lesson_number = item.get("lesson_number") or item.get("ordinal")
            
            # Преобразуем время: сначала проверяем массив time [h, m, s] или iso_date_time
            if not begin_time:
                time_arr = item.get("time")
                if isinstance(time_arr, list) and len(time_arr) >= 2:
                    h, m = time_arr[0], time_arr[1]
                    begin_time = f"{h:02d}:{m:02d}"
                    duration = item.get("duration") or 45
                    end_m = m + duration
                    end_h = h + end_m // 60
                    end_m = end_m % 60
                    end_time = f"{end_h:02d}:{end_m:02d}"
                elif item.get("iso_date_time"):
                    try:
                        iso_dt = datetime.fromisoformat(item["iso_date_time"].replace("Z", ""))
                        begin_time = iso_dt.strftime("%H:%M")
                        duration = item.get("duration") or 45
                        end_dt = iso_dt + timedelta(minutes=duration)
                        end_time = end_dt.strftime("%H:%M")
                    except Exception:
                        pass
                elif lesson_number and lesson_number in LESSON_NUMBER_TO_TIME:
                    lesson_time = LESSON_NUMBER_TO_TIME[lesson_number]
                    begin_time = lesson_time.strftime("%H:%M")
                    start_dt = datetime.combine(date.today(), lesson_time)
                    end_dt = start_dt + timedelta(minutes=LESSON_DURATION_MINUTES)
                    end_time = end_dt.time().strftime("%H:%M")
                elif lesson_number:
                    begin_time = f"Урок {lesson_number}"
                    end_time = ""
            
            theme_id = None
            if item.get("lesson_plan_items"):
                theme_id = item["lesson_plan_items"][0].get("theme_frame_integration_id")
            
            lessons.append({
                "schedule_lesson_id": item.get("id"),
                "group_name": group_name,
                "group_id": group_id,
                "subject_name": subject_name,
                "subject_id": subject_id,
                "time_start": begin_time,
                "time_end": end_time,
                "lesson_number": lesson_number,
                "theme_id": theme_id
            })
        
        # Сортируем уроки по времени начала
        lessons.sort(key=lambda x: x.get('lesson_number') or 0)
            
        return lessons

    def find_current_lesson(self, lessons: List[Dict]) -> Optional[Dict]:
        """
        Найти текущий урок по времени.
        
        Args:
            lessons: Список уроков из get_daily_schedule()
        
        Returns:
            Текущий урок или None
        """
        now = datetime.now().time()
        
        for lesson in lessons:
            start_time = parse_time_from_string(lesson.get('time_start', ''))
            if not start_time:
                continue
            
            # Вычисляем время окончания урока
            start_dt = datetime.combine(date.today(), start_time)
            end_dt = start_dt + timedelta(minutes=LESSON_DURATION_MINUTES)
            end_time = end_dt.time()
            
            # Проверяем, попадает ли текущее время в урок
            if start_time <= now <= end_time:
                return lesson
        
        # Если урок не идёт, проверяем перемену (ближайший урок в пределах 15 мин)
        now_dt = datetime.combine(date.today(), now)
        closest_lesson = None
        min_delta = timedelta(minutes=15)
        
        for lesson in lessons:
            start_time = parse_time_from_string(lesson.get('time_start', ''))
            if not start_time:
                continue
            
            start_dt = datetime.combine(date.today(), start_time)
            delta = start_dt - now_dt
            
            if timedelta(0) <= delta < min_delta:
                min_delta = delta
                closest_lesson = lesson
        
        return closest_lesson

    def find_lessons_by_class(self, lessons: List[Dict], class_name: str) -> List[Dict]:
        """
        Найти все уроки для данного класса.
        
        Args:
            lessons: Список уроков
            class_name: Название класса (например "7А" или "8Б")
        
        Returns:
            Список совпадающих уроков
        """
        class_lower = class_name.lower()
        return [l for l in lessons if class_lower in l['group_name'].lower()]


def get_profile_id() -> Optional[str]:
    """
    Получить Profile ID учителя.
    
    Profile ID (teacher_id) - это ID профиля учителя в МЭШ.
    Он отличается от user_id в токене!
    
    Порядок получения:
    1. Переменная окружения MESH_PROFILE_ID
    2. Файл ~/.mesh_profile_id
    3. Запрос у пользователя (с сохранением в файл)
    
    Как узнать свой Profile ID:
    1. Откройте браузер DevTools (F12) на school.mos.ru
    2. Перейдите в Network → любой API запрос
    3. Найдите заголовок 'Profile-Id' в Request Headers
    
    Returns:
        Profile ID как строка или None
    """
    profile_file = os.path.expanduser("~/.mesh_profile_id")
    
    # 1. Проверяем переменную окружения
    profile_id = os.environ.get("MESH_PROFILE_ID")
    if profile_id:
        print(f"✅ Profile ID загружен из MESH_PROFILE_ID: {profile_id}")
        return profile_id
    
    # 2. Проверяем файл
    if os.path.exists(profile_file):
        try:
            with open(profile_file, "r") as f:
                profile_id = f.read().strip()
                if profile_id:
                    print(f"✅ Profile ID загружен из {profile_file}: {profile_id}")
                    return profile_id
        except Exception as e:
            print(f"⚠️  Ошибка чтения {profile_file}: {e}")
    
    # 3. Запрашиваем у пользователя
    print("\n" + "=" * 60)
    print("⚠️  Profile ID не найден!")
    print("=" * 60)
    print("\nКак узнать свой Profile ID:")
    print("1. Откройте https://school.mos.ru и войдите как учитель")
    print("2. Нажмите F12 → вкладка Network")
    print("3. Обновите страницу и найдите любой запрос к API")
    print("4. В заголовках запроса найдите 'Profile-Id'")
    print()
    
    profile_id = input("Введите ваш Profile ID: ").strip()
    
    if not profile_id:
        print("❌ Profile ID не указан!")
        return None
    
    # Сохраняем для будущих запусков
    try:
        with open(profile_file, "w") as f:
            f.write(profile_id)
        os.chmod(profile_file, 0o600)
        print(f"✅ Profile ID сохранён в {profile_file}")
    except Exception as e:
        print(f"⚠️  Не удалось сохранить Profile ID: {e}")
    
    return profile_id


try:
    import browser_cookie3
    HAS_BROWSER_COOKIE3 = True
except ImportError:
    HAS_BROWSER_COOKIE3 = False


def get_token_from_browser() -> Optional[str]:
    """Попытаться получить токен авторизации из браузера Chrome.
    
    Приоритет:
    1. Cookie 'aupd_token' - это правильный токен для API МЭШ
    2. Другие JWT cookies (на всякий случай)
    
    ВАЖНО: Токены из localStorage НЕ используются, т.к. они имеют другой формат
    и не работают с API, даже если срок их действия дольше.
    """
    
    def is_token_valid(token: str) -> bool:
        """Проверить, что JWT токен не истёк."""
        import base64
        try:
            parts = token.split('.')
            if len(parts) >= 2:
                payload = parts[1]
                # Добавляем padding
                payload += '=' * (4 - len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                data = json.loads(decoded)
                exp = data.get('exp')
                if exp:
                    exp_time = datetime.fromtimestamp(exp)
                    if exp_time > datetime.now():
                        return True
            return False
        except Exception:
            return False
    
    def get_token_expiry(token: str) -> Optional[datetime]:
        """Получить время истечения токена."""
        import base64
        try:
            parts = token.split('.')
            if len(parts) >= 2:
                payload = parts[1]
                payload += '=' * (4 - len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                data = json.loads(decoded)
                exp = data.get('exp')
                if exp:
                    return datetime.fromtimestamp(exp)
        except Exception:
            pass
        return None
    
    def get_token_from_helium() -> Optional[str]:
        """Извлечь токен aupd_token из браузера Helium на macOS."""
        import glob, tempfile, shutil, sqlite3, subprocess, hashlib
        try:
            from Cryptodome.Cipher import AES
        except ImportError:
            try:
                from Crypto.Cipher import AES
            except ImportError:
                return None

        try:
            p = subprocess.run(
                ['security', 'find-generic-password', '-w', '-s', 'Helium Storage Key'],
                capture_output=True, text=True
            )
            pw = p.stdout.strip()
            if not pw or p.returncode != 0:
                return None
            key = hashlib.pbkdf2_hmac('sha1', pw.encode(), b'saltysalt', 1003, 16)
            helium_dir = os.path.expanduser('~/Library/Application Support/net.imput.helium')
            dbs = glob.glob(os.path.join(helium_dir, '*', 'Cookies'))
            if not dbs:
                return None

            tokens = {}
            for db in dbs:
                tmp = os.path.join(tempfile.mkdtemp(), 'Cookies')
                try:
                    shutil.copy2(db, tmp)
                    con = sqlite3.connect(tmp)
                    con.text_factory = bytes
                    for host, name, value, enc in con.execute('SELECT host_key, name, value, encrypted_value FROM cookies'):
                        h = host.decode('utf-8', 'ignore') if isinstance(host, bytes) else host
                        n = name.decode('utf-8', 'ignore') if isinstance(name, bytes) else name
                        if 'mos.ru' in h and n in ('aupd_token', 'aupd_refresh_token'):
                            val = ""
                            if enc and enc[:3] in (b'v10', b'v11'):
                                cipher = AES.new(key, AES.MODE_CBC, iv=b' ' * 16)
                                pt = cipher.decrypt(enc[3:])
                                pad = pt[-1] if pt else 0
                                if 1 <= pad <= 16:
                                    pt = pt[:-pad]
                                try:
                                    val = pt.decode('utf-8')
                                except UnicodeDecodeError:
                                    val = pt[32:].decode('utf-8', 'ignore')
                            elif value:
                                val = value.decode('utf-8', 'ignore') if isinstance(value, bytes) else str(value)
                            if val and val.startswith('eyJ'):
                                tokens[n] = val
                    con.close()
                finally:
                    shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)

            if 'aupd_refresh_token' in tokens:
                rt_file = os.path.expanduser('~/.mesh_refresh_token')
                with open(rt_file, 'w') as f:
                    f.write(tokens['aupd_refresh_token'])
                os.chmod(rt_file, 0o600)
                print("   ✅ Свежий refresh-токен сохранён из Helium в ~/.mesh_refresh_token")

            if 'aupd_token' in tokens and is_token_valid(tokens['aupd_token']):
                token = tokens['aupd_token']
                exp = get_token_expiry(token)
                print(f"✅ Используется aupd_token из Helium (истекает: {exp})")
                return token
        except Exception as e:
            print(f"⚠️  Ошибка извлечения токенов из Helium: {e}")
        return None

    # 1. Сначала пробуем Helium (основной браузер на macOS)
    print("🔍 Ищу токен aupd_token в cookies Helium...")
    helium_token = get_token_from_helium()
    if helium_token:
        return helium_token

    # 2. Ищем токен в cookies Chrome (как запасной вариант)
    if HAS_BROWSER_COOKIE3:
        try:
            print("🔍 Ищу токен aupd_token в cookies Chrome...")
            cj = browser_cookie3.chrome(domain_name='mos.ru')
            
            aupd_token = None
            other_jwt_tokens = []
            
            for cookie in cj:
                # Приоритет: ищем именно aupd_token
                if cookie.name == 'aupd_token':
                    if cookie.value.startswith('eyJ') and is_token_valid(cookie.value):
                        exp = get_token_expiry(cookie.value)
                        aupd_token = (cookie.value, exp)
                        print(f"   ✅ Найден aupd_token (истекает: {exp})")
                # Другие JWT токены как запасной вариант
                elif cookie.value.startswith('eyJ') and len(cookie.value) > 200:
                    if is_token_valid(cookie.value):
                        exp = get_token_expiry(cookie.value)
                        other_jwt_tokens.append((cookie.value, exp, cookie.name))
                        print(f"   ℹ️  Найден другой JWT токен '{cookie.name}' (истекает: {exp})")
            
            # Возвращаем aupd_token если есть
            if aupd_token:
                print(f"✅ Используется aupd_token из cookies (истекает: {aupd_token[1]})")
                return aupd_token[0]
            
            # Иначе используем другой JWT токен
            if other_jwt_tokens:
                # Сортируем по времени истечения
                other_jwt_tokens.sort(key=lambda x: x[1] if x[1] else datetime.min)
                best = other_jwt_tokens[-1]
                print(f"⚠️  aupd_token не найден, используется '{best[2]}' (истекает: {best[1]})")
                return best[0]
                
        except Exception as e:
            print(f"⚠️  browser_cookie3 ошибка: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("⚠️  browser_cookie3 не установлен! Установите: pip install browser_cookie3")
    
    print("⚠️  Действующие токены не найдены в cookies браузера.")
    print("   Убедитесь, что вы авторизованы в https://school.mos.ru")
    return None


def refresh_mesh_token() -> Optional[str]:
    """Ротация токена через штатный POST /v3/token/refresh.

    Refresh-токен ОДНОРАЗОВЫЙ — при успехе сохраняем ОБА новых токена.
    Возвращает новый access_token или None.
    """
    rt_file = os.path.expanduser("~/.mesh_refresh_token")
    token_file = os.path.expanduser("~/.mesh_token")
    if not os.path.exists(rt_file):
        return None
    try:
        rt = open(rt_file).read().strip()
        if not rt:
            return None
        resp = requests.post(
            "https://school.mos.ru/v3/token/refresh",
            json={"refresh_token": rt},
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                     "Cookie": "aupd_refresh_token=" + rt},
            timeout=30)
        if resp.status_code != 201:
            print(f"⚠️  refresh: HTTP {resp.status_code}: {resp.text[:150]}")
            return None
        d = resp.json()
        open(token_file, "w").write(d["access_token"])
        os.chmod(token_file, 0o600)
        open(rt_file, "w").write(d["refresh_token"])  # сразу оба — refresh одноразовый!
        os.chmod(rt_file, 0o600)
        print("✅ Токен обновлён через /v3/token/refresh")
        return d["access_token"]
    except Exception as e:
        print(f"⚠️  refresh не удался: {e}")
        return None


def get_token(force_refresh: bool = False) -> str:
    """Получить токен авторизации.

    Порядок: MESH_TOKEN → ~/.mesh_token (актуальный путь: Helium + refresh)
    → браузер → ручной ввод. При force_refresh сначала пробуем /v3/token/refresh.
    """
    token_file = os.path.expanduser("~/.mesh_token")

    if force_refresh:
        token = refresh_mesh_token()
        if token:
            return token

    if not force_refresh:
        token = os.environ.get("MESH_TOKEN")
        if token:
            print("✅ Токен загружен из переменной MESH_TOKEN")
            return token

        # Сначала сохранённый токен (актуальный путь: Helium + refresh)
        if os.path.exists(token_file):
            with open(token_file, "r") as f:
                token = f.read().strip()
                if token:
                    print(f"✅ Токен загружен из {token_file}")
                    return token

    print("\n🔄 Попытка автоматического получения токена из браузера...")
    token = get_token_from_browser()
    if token:
        print("✅ Токен успешно получен из браузера!")
        with open(token_file, "w") as f:
            f.write(token)
        os.chmod(token_file, 0o600)
        return token
    
    print("\n" + "=" * 60)
    print("⚠️  Токен авторизации не найден автоматически!")
    print("=" * 60)
    print("\nКак получить токен:")
    print("1. Откройте https://school.mos.ru и войдите")
    print("2. Нажмите F12 → вкладка Network")
    print("3. Перейдите в 'Кабинет учителя'")
    print("4. Найдите любой запрос к school.mos.ru/api/...")
    print("5. Скопируйте Headers → Authorization (без 'Bearer ')")
    print()
    
    token = input("Вставьте токен (или Enter для выхода): ").strip()
    
    if not token:
        print("Выход.")
        exit(0)
    
    if token.startswith("Bearer "):
        token = token[7:]
    
    with open(token_file, "w") as f:
        f.write(token)
    os.chmod(token_file, 0o600)
    print(f"✅ Токен сохранён в {token_file}")
    
    return token


def main():
    print("=" * 60)
    print("🤖 MESH Grade Automation V2 (Расширенный режим)")
    print("=" * 60)
    print("Новые функции:")
    print("  • Автоматический выбор урока по времени")
    print("  • Пакетное выставление оценок")
    print("  • Пакетная отметка отсутствия")
    print("=" * 60)
    
    # 1. Авторизация
    bearer_token = get_token()
    mesh = MeshAutomation(bearer_token=bearer_token)  # profile_id извлекается из токена автоматически
    
    try:
        # 2. Получаем расписание на сегодня
        print("\n📅 Загружаю расписание на сегодня...")
        lessons = mesh.get_daily_schedule()
        
        if not lessons:
            print("❌ На сегодня уроков не найдено.")
            return

        print(f"✅ Найдено уроков: {len(lessons)}")
        for i, l in enumerate(lessons, 1):
            time_str = l.get('time_start', '?')
            print(f"   {i}. {time_str} | {l['group_name']} | {l['subject_name']}")

        # 3. Автоматический выбор или ручной ввод
        print("\n" + "-" * 40)
        current_lesson = mesh.find_current_lesson(lessons)
        
        if current_lesson:
            print(f"⏰ Текущий урок (по времени): {current_lesson['group_name']} ({current_lesson['time_start']})")
            use_current = input("   Использовать этот урок? (Enter=да, или введите класс): ").strip()
            
            if use_current:
                # Пользователь хочет выбрать другой класс
                matching_lessons = mesh.find_lessons_by_class(lessons, use_current)
                if not matching_lessons:
                    print(f"❌ Урок для класса '{use_current}' сегодня не найден!")
                    return
                elif len(matching_lessons) == 1:
                    found_lesson = matching_lessons[0]
                else:
                    # Несколько уроков в одном классе - выбираем по времени или вручную
                    print(f"\n⚠️  Найдено несколько уроков для класса '{use_current}':")
                    for i, l in enumerate(matching_lessons, 1):
                        print(f"   {i}. {l['time_start']} | {l['group_name']} | {l['subject_name']}")
                    
                    choice = input("   Выберите номер урока: ").strip()
                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(matching_lessons):
                            found_lesson = matching_lessons[idx]
                        else:
                            print("❌ Неверный номер!")
                            return
                    except ValueError:
                        print("❌ Введите число!")
                        return
            else:
                found_lesson = current_lesson
        else:
            # Нет текущего урока по времени, запрашиваем ввод
            print("⏰ Текущий урок не определён (вне расписания)")
            target_class = input("   Введите класс (например '7А'): ").strip()
            
            if not target_class:
                print("Выход.")
                return
            
            matching_lessons = mesh.find_lessons_by_class(lessons, target_class)
            if not matching_lessons:
                print(f"❌ Урок для класса '{target_class}' сегодня не найден!")
                return
            elif len(matching_lessons) == 1:
                found_lesson = matching_lessons[0]
            else:
                print(f"\n⚠️  Найдено несколько уроков для класса '{target_class}':")
                for i, l in enumerate(matching_lessons, 1):
                    print(f"   {i}. {l['time_start']} | {l['group_name']} | {l['subject_name']}")
                
                choice = input("   Выберите номер урока: ").strip()
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(matching_lessons):
                        found_lesson = matching_lessons[idx]
                    else:
                        print("❌ Неверный номер!")
                        return
                except ValueError:
                    print("❌ Введите число!")
                    return

        print(f"\n✅ Выбран урок: {found_lesson['group_name']} ({found_lesson['time_start']})")

        # 4. Выбор действия
        print("\n" + "-" * 40)
        print("Выберите действие:")
        print("  1. Выставить оценку (одному ученику)")
        print("  2. Выставить оценку нескольким ученикам")
        print("  3. Отметить отсутствие (Н) нескольким ученикам")
        
        action = input("\nДействие (1/2/3): ").strip()

        if action == "1":
            # Одиночная оценка
            target_student = input("Фамилия ученика: ").strip()
            if not target_student:
                return
            
            try:
                grade_val = int(input("Оценка (2-5): ").strip())
            except ValueError:
                print("❌ Оценка должна быть числом!")
                return
            
            student = mesh.find_student_by_name(found_lesson['group_id'], target_student)
            if not student:
                print(f"❌ Ученик '{target_student}' не найден!")
                return
            
            process_single_grade(mesh, found_lesson, student, grade_val)

        elif action == "2":
            # Пакетные оценки
            print("\nВведите фамилии учеников через запятую:")
            students_input = input("Ученики: ").strip()
            if not students_input:
                return
            
            names = [n.strip() for n in students_input.split(',')]
            
            try:
                grade_val = int(input("Оценка для всех (2-5): ").strip())
            except ValueError:
                print("❌ Оценка должна быть числом!")
                return
            
            found_students, not_found = mesh.find_students_by_names(found_lesson['group_id'], names)
            
            if not_found:
                print(f"\n⚠️  Не найдены: {', '.join(not_found)}")
            
            if found_students:
                print(f"\n✅ Найдено {len(found_students)} учеников:")
                for s in found_students:
                    print(f"   - {s.get('short_name') or s.get('user_name')}")
                
                process_batch_grades(mesh, found_lesson, found_students, grade_val)

        elif action == "3":
            # Пакетные Н
            print("\nВведите фамилии отсутствующих через запятую:")
            students_input = input("Отсутствующие: ").strip()
            if not students_input:
                return
            
            names = [n.strip() for n in students_input.split(',')]
            
            found_students, not_found = mesh.find_students_by_names(found_lesson['group_id'], names)
            
            if not_found:
                print(f"\n⚠️  Не найдены: {', '.join(not_found)}")
            
            if found_students:
                print(f"\n✅ Найдено {len(found_students)} учеников для отметки Н:")
                for s in found_students:
                    print(f"   - {s.get('short_name') or s.get('user_name')}")
                
                process_batch_absences(mesh, found_lesson, found_students)

        else:
            print("❌ Неверное действие!")

    except Exception as e:
        print(f"\n❌ Глобальная ошибка: {e}")
        import traceback
        traceback.print_exc()


def get_lesson_params(mesh: MeshAutomation, lesson: Dict) -> Tuple[Optional[int], Optional[int]]:
    """Получить control_form_id и theme_id для урока."""
    # Получаем формы контроля
    print(f"   🔍 Получаю формы контроля для предмета {lesson.get('subject_id')}...")
    forms = mesh.get_control_forms(lesson['subject_id'])
    control_form_id = None
    for form in forms:
        if "Учебное задание" in form.get('name', ''):
            control_form_id = form['id']
            print(f"   ✅ Найдена форма контроля: Учебное задание (ID: {control_form_id})")
            break
    if not control_form_id and forms:
        control_form_id = forms[0]['id']
        print(f"   ⚠️  Используется первая форма контроля: {forms[0].get('name')} (ID: {control_form_id})")
    
    if not control_form_id:
        print(f"   ❌ Формы контроля не найдены!")
    
    # Тема урока
    theme_id = lesson.get('theme_id')
    
    if theme_id:
        print(f"   ✅ Тема урока из расписания: {theme_id}")
    else:
        print(f"   🔍 Тема не привязана к уроку, ищу в расписании...")
        schedule_items = mesh.get_schedule_items(lesson['group_id'])
        for item in schedule_items:
            if item.get('id') == lesson['schedule_lesson_id']:
                if item.get('lesson_plan_items'):
                    theme_id = item['lesson_plan_items'][0].get('theme_frame_integration_id')
                    print(f"   ✅ Тема найдена в расписании: {theme_id}")
                break
    
    # Fallback: получаем первую тему из КТП группы
    if not theme_id:
        print(f"   🔍 Тема не найдена, ищу в КТП...")
        try:
            # Ищем КТП для нужного предмета
            subject_id = lesson.get('subject_id')
            
            # Получаем ВСЕ КТП учителя (без фильтра по group_id)
            calendar_plans = mesh.get_calendar_plans()
            
            # Ищем КТП для нужного предмета/группы
            ktp_list_for_subj = []
            matching_ktp = None
            lesson_group_id = lesson.get('group_id')
            
            # print(f"   🔍 DEBUG: Ищем КТП для subject_id={subject_id}, group_id={lesson_group_id}")
            # print(f"   🔍 DEBUG: Всего доступных КТП: {len(calendar_plans)}")
            for ktp_item in calendar_plans:
                # print(f"      - ID: {ktp_item.get('id')}, Grp: {ktp_item.get('group_name')} (ID: {ktp_item.get('group_id')})")
                # 1. Сначала пробуем найти точное совпадение по subject_id (если доступно)
                if ktp_item.get('subject_id') == subject_id:
                    matching_ktp = ktp_item
                    break
                
                # 2. Если не нашли, ищем по group_id (это надежнее)
                if lesson_group_id and ktp_item.get('group_id') == lesson_group_id:
                    matching_ktp = ktp_item
                    print(f"   ✅ Найдено КТП по совпадению группы (ID: {ktp_item.get('id')})")
                    break
            
            if matching_ktp:
                 print(f"   🔍 Выбрано КТП: {matching_ktp.get('id')}")
            else:
                 print(f"   ⚠️  КТП для предмета {subject_id} (группа {lesson_group_id}) не найдено")
            
            # Если не нашли по subject_id, берём первый доступный
            if not matching_ktp and calendar_plans:
                matching_ktp = calendar_plans[0]
                print(f"   ⚠️  Используется первый доступный КТП: {matching_ktp.get('subject_name', 'N/A')}")
            
            if matching_ktp:
                ktp_details = mesh.get_calendar_plan_details(matching_ktp.get('id'))
                if ktp_details:
                    # ОТЛАДКА: Выводим ключи и структуру для понимания
                    # print(f"   🔍 DEBUG: Keys in ktp_details: {list(ktp_details.keys())}")
                    # if 'theme_frames' in ktp_details:
                    #      print(f"   🔍 DEBUG: theme_frames count: {len(ktp_details['theme_frames'])}")
                    # elif 'lessons' in ktp_details and len(ktp_details['lessons']) > 0:
                    #     print(f"   🔍 DEBUG: First lesson in KTP: {ktp_details['lessons'][0]}")
                    
                    # Пробуем разные поля где может быть тема
                    theme_frames = ktp_details.get('theme_frames') or ktp_details.get('themes') or []
                    
                    if theme_frames:
                        for theme in theme_frames:
                            tid = theme.get('theme_frame_integration_id') or theme.get('id')
                            if tid:
                                theme_id = tid
                                print(f"   ✅ Тема из КТП: {theme.get('name', 'без названия')} (ID: {theme_id})")
                                break
                    else:
                        print(f"   ⚠️  КТП найден, но темы пустые. Проверьте КТП в журнале МЭШ.")
        except Exception as e:
            print(f"   ⚠️  Ошибка получения КТП: {e}")
    
    # Последний Fallback: hardcoded для Труда (для обратной совместимости)
    if not theme_id:
        subject_name = lesson.get('subject_name', '').lower()
        if 'труд' in subject_name or 'технолог' in subject_name:
            theme_id = 37585368  # theme_frame_id_robotics
            print(f"   ⚠️  Используется fallback тема для Труда (ID: {theme_id})")
    
    # Fallback: Интерактивный выбор темы из каркаса
    if not theme_id:
        print("   🔍 Тема не найдена в КТП. Загружаю тематический каркас...")
        try:
             subject_id = lesson.get('subject_id')
             frames = mesh.get_thematic_frames(subject_id)
             if frames:
                 print("\n   📋  Выберите тему из списка:")
                 valid_themes = []
                 for item in frames:
                     # Пытаемся получить название (title - основной ключ в каркасе)
                     name = item.get('title') or item.get('caption') or item.get('name') or item.get('topic_name') or "Без названия"
                     tid = item.get('id')
                     if tid and name != "Без названия":
                         valid_themes.append((tid, name))
                 
                 if not valid_themes:
                     print("   ⚠️  Список тем пуст.")
                 else:
                     # Автоматический выбор первой темы (без опроса пользователя)
                     theme_id = valid_themes[0][0]
                     theme_name = valid_themes[0][1]
                     print(f"   ✨ [AUTO] Тема урока выбрана из каркаса: {theme_name}")
                             
        except Exception as e:
             print(f"   ⚠️  Ошибка выбора темы из каркаса: {e}")
             
    if not theme_id:
        print(f"   ❌ Тема урока не найдена! Необходимо привязать тему в журнале МЭШ или обновить КТП.")
    
    return control_form_id, theme_id


def process_single_grade(mesh: MeshAutomation, lesson: Dict, student: Dict, grade: int):
    """Выставить оценку одному ученику."""
    control_form_id, theme_id = get_lesson_params(mesh, lesson)
    
    if not control_form_id or not theme_id:
        error_msg = "Не удалось получить параметры урока"
        if not control_form_id:
            error_msg += " (нет формы контроля)"
        if not theme_id:
            subj = lesson.get('subject_name', 'Неизвестно')
            error_msg += f" (нет темы урока для '{subj}')"
        print(f"❌ {error_msg}")
        raise Exception(error_msg)
    
    student_name = student.get('short_name') or student.get('user_name')
    
    result = mesh.add_mark(
        schedule_lesson_id=lesson['schedule_lesson_id'],
        student_profile_id=student['id'],
        teacher_id=int(mesh.profile_id),
        grade=grade,
        control_form_id=control_form_id,
        theme_frame_integration_id=theme_id
    )
    print(f"✅ Оценка выставлена! ID: {result.get('id')}")
    return result


def process_batch_grades(mesh: MeshAutomation, lesson: Dict, students: List[Dict], grade: int):
    """Выставить оценки нескольким ученикам."""
    control_form_id, theme_id = get_lesson_params(mesh, lesson)
    
    if not control_form_id or not theme_id:
        error_msg = "Не удалось получить параметры урока"
        if not control_form_id:
            error_msg += " (нет формы контроля)"
        if not theme_id:
            subj = lesson.get('subject_name', 'Неизвестно')
            error_msg += f" (нет темы урока для '{subj}')"
        print(f"❌ {error_msg}")
        raise Exception(error_msg)
    
    success = 0
    failed = 0
    
    for student in students:
        student_name = student.get('short_name') or student.get('user_name')
        try:
            result = mesh.add_mark(
                schedule_lesson_id=lesson['schedule_lesson_id'],
                student_profile_id=student['id'],
                teacher_id=int(mesh.profile_id),
                grade=grade,
                control_form_id=control_form_id,
                theme_frame_integration_id=theme_id
            )
            print(f"   ✅ {student_name}: оценка {grade}")
            success += 1
        except Exception as e:
            print(f"   ❌ {student_name}: ошибка - {e}")
            failed += 1
    
    print(f"\n📊 Итог: {success} успешно, {failed} ошибок")


def process_batch_absences(mesh: MeshAutomation, lesson: Dict, students: List[Dict]):
    """Отметить отсутствие нескольким ученикам."""
    
    success = 0
    failed = 0
    
    for student in students:
        student_name = student.get('short_name') or student.get('user_name')
        try:
            result = mesh.add_absence(
                schedule_lesson_id=lesson['schedule_lesson_id'],
                student_profile_id=student['id'],
                teacher_id=int(mesh.profile_id)
            )
            print(f"   ✅ {student_name}: отмечен Н")
            success += 1
        except Exception as e:
            print(f"   ❌ {student_name}: ошибка - {e}")
            failed += 1
    
    print(f"\n📊 Итог: {success} успешно, {failed} ошибок")


def update_all_ktp(mesh: MeshAutomation, auto_update: bool = False):
    """
    Обновить КТП (Календарно-тематическое планирование) для всех классов.
    
    Выполняет действие "Пересчитать даты" для каждого КТП учителя.
    
    Args:
        mesh: Экземпляр MeshAutomation
        auto_update: Если True, обновляет все КТП без запроса подтверждения
                    (для использования из голосового ассистента)
    """
    print("\n" + "=" * 60)
    print("📅 Обновление КТП (Календарно-тематическое планирование)")
    print("=" * 60)
    
    # 1. Получаем список КТП
    print("\n🔄 Загружаю список КТП...")
    calendar_plans = mesh.get_calendar_plans()
    
    if not calendar_plans:
        print("❌ КТП не найдены.")
        return
    
    print(f"✅ Найдено КТП: {len(calendar_plans)}")
    
    # Выводим список КТП
    print("\n📋 Список КТП:")
    for i, ktp in enumerate(calendar_plans, 1):
        ktp_id = ktp.get('id')
        group_name = ktp.get('group_name', 'Неизвестная группа')
        subject_name = ktp.get('subject_name', 'Неизвестный предмет')
        status = ktp.get('status', '')
        print(f"   {i}. [{ktp_id}] {group_name} | {subject_name} | Статус: {status}")
    
    # Если auto_update=True, пропускаем интерактивное меню
    if auto_update:
        print(f"\n🔄 Автоматическое обновление всех {len(calendar_plans)} КТП...")
        plans_to_update = calendar_plans
    else:
        # 2. Спрашиваем подтверждение (интерактивный режим)
        print("\n" + "-" * 40)
        print("Выберите действие:")
        print("  1. Обновить ВСЕ КТП (пересчитать даты)")
        print("  2. Выбрать конкретные КТП для обновления")
        print("  3. Отмена")
        
        choice = input("\nДействие (1/2/3): ").strip()
        
        if choice == "3" or not choice:
            print("Отменено.")
            return
        
        plans_to_update = []
        
        if choice == "1":
            plans_to_update = calendar_plans
            confirm = input(f"\n⚠️  Обновить ВСЕ {len(calendar_plans)} КТП? (да/нет): ").strip().lower()
            if confirm not in ['да', 'yes', 'y', 'д']:
                print("Отменено.")
                return
        elif choice == "2":
            print("\nВведите номера КТП для обновления через запятую (например: 1,3,5):")
            nums_input = input("Номера: ").strip()
            if not nums_input:
                print("Отменено.")
                return
            
            try:
                nums = [int(n.strip()) for n in nums_input.split(',')]
                for num in nums:
                    if 1 <= num <= len(calendar_plans):
                        plans_to_update.append(calendar_plans[num - 1])
                    else:
                        print(f"⚠️  Номер {num} вне диапазона, пропускаю...")
            except ValueError:
                print("❌ Неверный формат!")
                return
            
            if not plans_to_update:
                print("❌ Не выбрано ни одного КТП!")
                return
        else:
            print("❌ Неверный выбор!")
            return
    
    # 3. Обновляем выбранные КТП
    print(f"\n🔄 Обновляю {len(plans_to_update)} КТП...")
    
    success = 0
    failed = 0
    max_retries = 3  # Максимум попыток для каждого КТП
        
    for ktp in plans_to_update:
        ktp_id = ktp.get('id')
        group_name = ktp.get('group_name', 'Неизвестная группа')
        
        print(f"\n   📝 Обновляю: {group_name} (ID: {ktp_id})...")
        
        # Пробуем несколько раз при ошибке
        update_success = False
        for attempt in range(1, max_retries + 1):
            if mesh.recalc_calendar_plan(ktp_id):
                print(f"   ✅ {group_name}: успешно обновлено!")
                update_success = True
                success += 1
                break
            else:
                if attempt < max_retries:
                    print(f"   ⚠️  Попытка {attempt}/{max_retries} не удалась, повторяю...")
                    _time.sleep(1)  # Пауза перед повторной попыткой
                else:
                    print(f"   ❌ {group_name}: ошибка обновления после {max_retries} попыток")
                    failed += 1
        
        # Небольшая пауза между запросами
        _time.sleep(0.5)
    
    print("\n" + "=" * 60)
    print(f"📊 Итог обновления КТП: {success} успешно, {failed} ошибок")
    print("=" * 60)


def publish_all_calendar_plans(mesh: MeshAutomation, academic_year_id: int = 14, auto_mode: bool = False):
    """
    Массово пересчитать, достроить даты и опубликовать ВСЕ КТП учителя.
    """
    print("\n" + "=" * 60)
    print("🚀 Массовая публикация и достройка КТП всех классов")
    print("=" * 60)
    
    calendar_plans = mesh.get_calendar_plans(academic_year_id=academic_year_id)
    if not calendar_plans:
        print("❌ КТП не найдены.")
        return
        
    print(f"✅ Найдено КТП: {len(calendar_plans)}")
    
    if not auto_mode:
        confirm = input(f"\n⚠️  Пересчитать, достроить даты и опубликовать ВСЕ {len(calendar_plans)} КТП? (да/нет): ").strip().lower()
        if confirm not in ['да', 'yes', 'y', 'д']:
            print("Отменено.")
            return
            
    success = 0
    failed = 0
    for idx, cp in enumerate(calendar_plans, 1):
        cpid = cp.get('id')
        gname = cp.get('group_name', 'Неизвестная группа')
        print(f"\n[{idx}/{len(calendar_plans)}] Публикую: {gname} (ID: {cpid})...")
        if mesh.publish_calendar_plan(cpid):
            success += 1
        else:
            failed += 1
        _time.sleep(0.3)
        
    print("\n" + "=" * 60)
    print(f"📊 Итог публикации КТП: {success} успешно, {failed} ошибок")
    print("=" * 60)


def create_lesson_plan_from_file(mesh: MeshAutomation, file_path: str = None):
    """Создать поурочный план из JSON файла шаблона."""
    if not file_path:
        file_path = input("Введите путь к JSON-файлу с планом: ").strip()
    if not os.path.exists(file_path):
        print(f"❌ Файл {file_path} не найден!")
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            plan_data = json.load(f)
            
        name = plan_data.get('name', 'Новый поурочный план')
        subject_id = plan_data.get('subject_id')
        class_level_id = plan_data.get('class_level_id', 9)
        subject_program_id = plan_data.get('subject_program_id')
        modules = plan_data.get('modules', [])
        status = plan_data.get('status', 'draft')
        week_hours = plan_data.get('week_hours', 1)
        
        result = mesh.create_lesson_plan(
            name=name,
            subject_id=subject_id,
            class_level_id=class_level_id,
            subject_program_id=subject_program_id,
            modules=modules,
            week_hours=week_hours,
            status=status
        )
        return result
    except Exception as e:
        print(f"❌ Ошибка чтения/создания плана из файла: {e}")
        return None



def set_homework_absences_all_groups(mesh: MeshAutomation, months: int = 2, auto_mode: bool = False):
    """
    Выставить отсутствие домашних заданий для всех групп учителя на N месяцев вперёд.

    Окно: от сегодня до конца загруженного расписания, но не дальше +months месяцев.
    Повторный прогон безопасен: уже проставленные даты пропускаются (400/500 от МЭШ
    распознаётся как "уже есть").

    Args:
        mesh: Экземпляр MeshAutomation
        months: Количество месяцев вперёд (по умолчанию 2)
        auto_mode: Если True, не запрашивает подтверждение (для голосового ассистента)
    """

    def add_months(d: datetime, n: int) -> datetime:
        """Прибавить n месяцев, не перескакивая через месяц (31.01 + 1 мес = 28/29.02)."""
        y, m = divmod(d.month - 1 + n, 12)
        y, m = d.year + y, m + 1
        last_day = calendar.monthrange(y, m)[1]
        return d.replace(year=y, month=m, day=min(d.day, last_day))

    print("\n" + "=" * 60)
    print("📝 Выставление отсутствия домашних заданий")
    print("=" * 60)

    # 1. Получаем список КТП учителя (только его классы)
    print("\n🔄 Загружаю список ваших классов (КТП)...")
    calendar_plans = mesh.get_calendar_plans()

    if not calendar_plans:
        print("❌ КТП не найдены.")
        return

    # Извлекаем уникальные группы из КТП
    groups = {}
    for ktp in calendar_plans:
        group_id = ktp.get('group_id')
        group_name = ktp.get('group_name', 'Без имени')
        if group_id and group_id not in groups:
            groups[group_id] = group_name

    print(f"✅ Найдено ваших классов: {len(groups)}")
    for i, (gid, gname) in enumerate(groups.items(), 1):
        print(f"   {i}. {gname} (ID: {gid})")

    # 2. Подтверждение (если не auto_mode)
    if not auto_mode:
        print(f"\n⚠️  Будет выставлено 'без д/з' для всех {len(groups)} классов на {months} месяца вперёд.")
        confirm = input("Продолжить? (да/нет): ").strip().lower()
        if confirm not in ['да', 'yes', 'y', 'д']:
            print("Отменено.")
            return
    else:
        print(f"\n🚀 Автоматический режим: выставляю 'без д/з' для {len(groups)} классов на {months} месяца...")

    # 3. Определяем окно дат
    today = datetime.now()
    date_from = today.strftime("%Y-%m-%d")
    date_to = add_months(today, months).strftime("%Y-%m-%d")

    date_from_ru = today.strftime("%d.%m.%Y")
    date_to_ru = add_months(today, months).strftime("%d.%m.%Y")

    print(f"\n📅 Период: {date_from_ru} - {date_to_ru}")

    total_success = 0
    total_failed = 0
    total_skipped = 0
    total_already = 0

    # 4. Обрабатываем каждую группу
    for group_id, group_name in groups.items():
        print(f"\n📚 Группа: {group_name} (ID: {group_id})")

        # 4.1 Получаем расписание уроков
        schedule_items = mesh.get_schedule_items_short(group_id, date_from)

        if not schedule_items:
            print(f"   ⚠️  Уроки не найдены для этой группы")
            continue

        # 4.2 Получаем существующие homework_absences (окно то же)
        existing_absences = mesh.get_homework_absences(group_id, date_from_ru, date_to_ru)
        existing_dates = {_hw_date_iso(a.get('date')) for a in existing_absences}
        existing_dates.discard(None)

        # 4.3 Собираем уникальные даты уроков (ISO) в границах окна
        lesson_dates = set()
        for item in schedule_items:
            d = _hw_date_iso(item.get('iso_date_time') if isinstance(item, dict) else item)
            if not d and isinstance(item, dict):
                d = _hw_date_iso(item.get('date') or item.get('lesson_date') or item.get('begin_date'))
            if d and date_from <= d <= date_to:
                lesson_dates.add(d)

        print(f"   📊 Дат уроков в окне: {len(lesson_dates)}, уже выставлено 'без д/з': {len(existing_dates)}")

        # 4.4 Находим даты без homework_absence
        dates_to_add = lesson_dates - existing_dates

        if not dates_to_add:
            print(f"   ✅ Все даты уже обработаны!")
            total_skipped += len(lesson_dates)
            continue

        print(f"   📝 Нужно добавить 'без д/з' на {len(dates_to_add)} дат...")

        # 4.5 Добавляем homework_absences
        success = 0
        failed = 0

        for lesson_date in sorted(dates_to_add):
            try:
                result = mesh.add_homework_absence(group_id, lesson_date)
                if isinstance(result, dict) and result.get('status') == 'already_exists':
                    total_already += 1
                else:
                    success += 1
                # Небольшая пауза чтобы не перегружать API
                _time.sleep(0.2)
            except Exception as e:
                print(f"   ❌ Ошибка для {lesson_date}: {e}")
                failed += 1

        print(f"   📊 Результат: {success} добавлено, {failed} ошибок")
        total_success += success
        total_failed += failed
        total_skipped += len(existing_dates & lesson_dates)

        # Пауза между группами
        _time.sleep(0.5)

    print("\n" + "=" * 60)
    print(f"📊 Итог: {total_success} добавлено, {total_skipped} пропущено (уже есть), "
          f"{total_already} обнаружено как 'уже есть' при вставке, {total_failed} ошибок")
    print("=" * 60)
def main_menu():
    """Главное меню приложения."""
    print("\n" + "=" * 60)
    print("🤖 MESH Grade Automation V3 (Расширенный режим)")
    print("=" * 60)
    print("Доступные функции:")
    print("  1. Выставление оценок / отметка отсутствия")
    print("  2. Обновить КТП всех классов (пересчитать даты)")
    print("  3. Выставить 'без домашнего задания' (2 месяца)")
    print("  4. Достроить и опубликовать ВСЕ КТП (recalc + finish + publish)")
    print("  5. Создать поурочный план из JSON-шаблона")
    print("  0. Выход")
    print("=" * 60)
    
    return input("\nВыберите действие (0/1/2/3/4/5): ").strip()


if __name__ == "__main__":
    # Авторизация
    bearer_token = get_token()
    mesh = MeshAutomation(bearer_token=bearer_token)  # profile_id извлекается из токена автоматически
    
    while True:
        choice = main_menu()
        
        if choice == "0":
            print("👋 До свидания!")
            break
        elif choice == "1":
            main()
        elif choice == "2":
            update_all_ktp(mesh)
        elif choice == "3":
            set_homework_absences_all_groups(mesh)
        elif choice == "4":
            publish_all_calendar_plans(mesh)
        elif choice == "5":
            create_lesson_plan_from_file(mesh)
        else:
            print("❌ Неверный выбор!")


