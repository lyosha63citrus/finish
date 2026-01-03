# -*- coding: utf-8 -*-
# VK-bot расписания (2 категории) + Render health-check + GitHub Gist persistence
#
# ВАЖНО (фикс):
# - "Ученики" / "Незаписавшиеся" / "Редактировать" берут список НЕ из known_users,
#   а из реального списка участников сообщества через user_token: groups.getMembers.
# - Никаких удалений записей при нажатии "Ученики".
#
# Админка:
# Админам -> Панель администратора:
#   - Ученики
#   - Админы
#   - Незаписавшиеся ученики
#   - Редактировать -> (Записать/Удалить) -> (Программирование/Бухгалтерия) -> список с номерами
#       Записать: выбираем ученика номером -> выбираем слот номером -> запись
#       Удалить: выбираем ученика номером -> удаление из предмета (все слоты предмета)
#   - Инструкция (админ)
#
# Админ-команды текстом:
#   /setxpr N d t CAP LIMIT      (точечно)
#   /setxbh N d t CAP LIMIT
#   /setxpr d1 t1 [d2 t2 ...] CAP LIMIT   (массово)
#   /setxbh d1 t1 [d2 t2 ...] CAP LIMIT
#   /delpr N   /delbh N          (удалить слот без сдвига: очищает title+users только этого слота)
#   /clearpr   /clearbh          (очистить все записи категории)
#
# Ученикам в расписании НЕ показываем номера слотов.

import os
import json
import time
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
import vk_api
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.exceptions import ApiError

load_dotenv()

# ───────────────── Health-check HTTP server for Render ─────────────────
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        return

def _start_health_server():
    try:
        port = int(os.environ.get("PORT", "10000"))
        srv = HTTPServer(("", port), _HealthHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Health server listening on :{port}")
    except Exception as e:
        print("Health server failed:", e)

_start_health_server()

# ───────────────── Gist persistence (optional) ─────────────────
import urllib.request
import json as _json

GIST_TOKEN = os.getenv("GIST_TOKEN")
GIST_ID = os.getenv("GIST_ID")

def _gist_headers():
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "vk-bot-schedule"
    }

def gist_load(filename: str) -> Optional[dict]:
    if not (GIST_TOKEN and GIST_ID):
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gist_headers()
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = _json.loads(r.read().decode("utf-8"))
        files = data.get("files", {})
        if filename in files and "content" in files[filename]:
            content = files[filename]["content"] or "{}"
            return _json.loads(content)
    except Exception as e:
        print("Gist load error:", e)
    return None

def gist_save(filename: str, obj: dict) -> None:
    if not (GIST_TOKEN and GIST_ID):
        return
    try:
        body = _json.dumps({
            "files": {
                filename: {
                    "content": _json.dumps(obj, ensure_ascii=False, indent=2)
                }
            }
        }).encode("utf-8")

        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            data=body,
            method="PATCH",
            headers=_gist_headers()
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("Gist save error:", e)

# ───────────── env ─────────────
COMMUNITY_TOKEN = os.getenv("VK_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
USER_TOKEN = os.getenv("USER_TOKEN")         # ВАЖНО: нужен для выгрузки участников
MASTER_ID_ENV = os.getenv("ADMIN_USER_ID")   # VK user_id (число)

if not COMMUNITY_TOKEN or not GROUP_ID:
    raise RuntimeError("Нет VK_TOKEN или GROUP_ID в .env")

# ───────────── VK ─────────────
vk_session = vk_api.VkApi(token=COMMUNITY_TOKEN)
session_api = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

user_api = None
if USER_TOKEN:
    try:
        user_session = vk_api.VkApi(token=USER_TOKEN)
        user_api = user_session.get_api()
        info2 = user_api.groups.getById(group_id=GROUP_ID)
        print("OK: USER_TOKEN видит группу:", info2[0]["name"])
    except Exception as e:
        print("Проблема с USER_TOKEN:", e)
else:
    print("⚠️ USER_TOKEN не указан. Списки участников (Ученики/Незаписавшиеся/Редактировать) будут работать хуже.")

# ───────────── категории / команды ─────────────
CAT_PR = "Программирование"
CAT_BH = "Бухгалтерия"
CATEGORIES = [CAT_PR, CAT_BH]

CMD_SET_PR = "/setxpr"
CMD_SET_BH = "/setxbh"
CMD_CLEAR_PR = "/clearpr"
CMD_CLEAR_BH = "/clearbh"
CMD_DEL_PR = "/delpr"
CMD_DEL_BH = "/delbh"

SLOT_KEYS = ["S1", "S2", "S3", "S4"]

# ───────────── state ─────────────
STATE_FILE = "state.json"

def _default_category_cfg() -> Dict:
    return {
        "capacity": 13,
        "limit_per_user": 1,
        "slots": [{"key": k, "title": "", "users": []} for k in SLOT_KEYS]
    }

def default_state() -> Dict:
    return {
        "known_users": {},  # "uid": {"name": "Имя Фамилия"} (оставим — полезно, но не используем как источник "учеников")
        "categories": {
            CAT_PR: _default_category_cfg(),
            CAT_BH: _default_category_cfg(),
        }
    }

def _normalize_state(data: dict) -> dict:
    if not isinstance(data, dict):
        return default_state()

    data.setdefault("known_users", {})
    if isinstance(data["known_users"], dict):
        for k, v in list(data["known_users"].items()):
            if isinstance(v, str):
                data["known_users"][k] = {"name": v}
            elif isinstance(v, dict):
                v.setdefault("name", "")
            else:
                data["known_users"].pop(k, None)
    else:
        data["known_users"] = {}

    data.setdefault("categories", {})
    if not isinstance(data["categories"], dict):
        data["categories"] = {}

    for cat in CATEGORIES:
        if cat not in data["categories"] or not isinstance(data["categories"][cat], dict):
            data["categories"][cat] = _default_category_cfg()

        cfg = data["categories"][cat]
        cfg.setdefault("capacity", 13)
        cfg.setdefault("limit_per_user", 1)
        cfg.setdefault("slots", [])
        if not isinstance(cfg["slots"], list):
            cfg["slots"] = []

        old_slots = cfg["slots"]
        key_to_slot = {}
        for idx, s in enumerate(old_slots):
            if not isinstance(s, dict):
                continue
            title = s.get("title", "")
            users = s.get("users", [])
            if not isinstance(users, list):
                users = []
            key = s.get("key")
            if not key:
                if idx < len(SLOT_KEYS):
                    key = SLOT_KEYS[idx]
                else:
                    continue
            key = str(key)
            if key in SLOT_KEYS:
                key_to_slot[key] = {"key": key, "title": str(title), "users": users}

        new_slots = []
        for k in SLOT_KEYS:
            new_slots.append(key_to_slot.get(k) or {"key": k, "title": "", "users": []})
        cfg["slots"] = new_slots

    return data

def load_state() -> Dict:
    g = gist_load(STATE_FILE)
    if g is not None:
        print("✓ Загружено состояние из Gist")
        return _normalize_state(g)

    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _normalize_state(data)
    except Exception:
        return default_state()

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    gist_save(STATE_FILE, state)

state = load_state()

# ───────────── админы ─────────────
MASTER_ID: Optional[int] = int(MASTER_ID_ENV) if (MASTER_ID_ENV and MASTER_ID_ENV.isdigit()) else None
# оставил твои id как в main(4).py
ADMINS: List[int] = [aid for aid in [MASTER_ID, 1080975674, 20158141] if isinstance(aid, int)]

# ───────────── runtime ─────────────
pending_cat: Dict[int, str] = {}
pending_rewrite: Dict[int, str] = {}   # user_id -> "menu"
admin_mode: Dict[int, str] = {}        # user_id -> "" | "panel" | "edit"

# админ-сценарий редактирования
# user_id -> {"step": "op"|"cat"|"pick_student"|"pick_slot", "op":"add"|"del", "cat":..., "students":[name..], "student":...}
admin_edit: Dict[int, Dict] = {}

# ───────────── клавиатуры ─────────────
def base_keyboard(is_admin: bool) -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Выбрать", VkKeyboardColor.POSITIVE)
    kb.add_button("Расписание", VkKeyboardColor.PRIMARY)
    kb.add_button("Мои записи", VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button("Инструкция", VkKeyboardColor.SECONDARY)
    kb.add_button("Админам", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Перезапись", VkKeyboardColor.PRIMARY)
    return kb

def schedule_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Подробно", VkKeyboardColor.PRIMARY)
    kb.add_button("Назад", VkKeyboardColor.SECONDARY)
    return kb

def choose_category_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button(CAT_PR, VkKeyboardColor.PRIMARY)
    kb.add_button(CAT_BH, VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    return kb

def slots_keyboard(cat: str) -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    for s in state["categories"][cat]["slots"]:
        title = (s.get("title") or "").strip()
        if title:
            kb.add_button(title, VkKeyboardColor.SECONDARY)
            kb.add_line()
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    return kb

def rewrite_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Перезапись: Программирование", VkKeyboardColor.PRIMARY)
    kb.add_button("Перезапись: Бухгалтерия", VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Перезапись: Всё", VkKeyboardColor.NEGATIVE)
    kb.add_button("Назад", VkKeyboardColor.SECONDARY)
    return kb

def admin_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Ученики", VkKeyboardColor.SECONDARY)
    kb.add_button("Админы", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Незаписавшиеся ученики", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Редактировать", VkKeyboardColor.PRIMARY)
    kb.add_button("Инструкция (админ)", VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Назад", VkKeyboardColor.NEGATIVE)
    return kb

def admin_edit_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Записать", VkKeyboardColor.POSITIVE)
    kb.add_button("Удалить", VkKeyboardColor.NEGATIVE)
    kb.add_line()
    kb.add_button("Назад", VkKeyboardColor.SECONDARY)
    return kb

def admin_edit_cat_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button(CAT_PR, VkKeyboardColor.PRIMARY)
    kb.add_button(CAT_BH, VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    kb.add_button("Назад", VkKeyboardColor.SECONDARY)
    return kb

# ───────────── сервис ─────────────
def send_msg(user_id: int, text: str, kb: Optional[VkKeyboard] = None):
    payload = {"user_id": user_id, "message": text, "random_id": 0}

    if kb is not None:
        payload["keyboard"] = kb.get_keyboard()
    else:
        mode = admin_mode.get(user_id, "")
        if mode == "panel":
            payload["keyboard"] = admin_keyboard().get_keyboard()
        elif mode == "edit":
            payload["keyboard"] = admin_edit_keyboard().get_keyboard()
        else:
            payload["keyboard"] = base_keyboard(user_id in ADMINS).get_keyboard()

    session_api.messages.send(**payload)

def roster_with_numbers(users: List[str]) -> str:
    if not users:
        return "—"
    return "\n".join(f"{i+1}. {u}" for i, u in enumerate(users))

def count_user_bookings_in_category(fullname: str, cat: str) -> int:
    return sum(1 for s in state["categories"][cat]["slots"] if fullname in s["users"])

def remove_user_from_category(fullname: str, cat: str) -> int:
    removed = 0
    for s in state["categories"][cat]["slots"]:
        if fullname in s["users"]:
            s["users"].remove(fullname)
            removed += 1
    return removed

def remove_user_from_all_categories(fullname: str) -> int:
    removed = 0
    for cat in CATEGORIES:
        removed += remove_user_from_category(fullname, cat)
    return removed

# ───────────── Расписание (без номеров слотов) ─────────────
def schedule_summary_text() -> str:
    lines: List[str] = ["📅 Расписание (кратко)\n"]
    for cat in CATEGORIES:
        cfg = state["categories"][cat]
        cap = int(cfg.get("capacity", 13))
        lines.append(f"🖥 {cat}")
        any_visible = False
        for s in cfg.get("slots", []):
            title = (s.get("title") or "").strip()
            if not title:
                continue
            any_visible = True
            taken = len(s.get("users", []))
            free = max(cap - taken, 0)
            lines.append(f"{title} | занято: {taken}/{cap} | свободно: {free}")
        if not any_visible:
            lines.append("Слоты не настроены.\n")
        lines.append("")
    lines.append("Нажмите «Подробно», чтобы увидеть списки записанных.")
    return "\n".join(lines).strip()

def schedule_detailed_text() -> str:
    lines: List[str] = ["📅 Расписание (подробно)\n"]
    for cat in CATEGORIES:
        cfg = state["categories"][cat]
        cap = int(cfg.get("capacity", 13))
        lines.append(f"🖥 {cat}")
        any_visible = False
        for s in cfg.get("slots", []):
            title = (s.get("title") or "").strip()
            if not title:
                continue
            any_visible = True
            users = s.get("users", [])
            taken = len(users)
            free = max(cap - taken, 0)
            lines.append(f"{title} | занято: {taken}/{cap} | свободно: {free}\n")
            lines.append(roster_with_numbers(users))
            lines.append("")
        if not any_visible:
            lines.append("Слоты не настроены.\n")
        lines.append("")
    return "\n".join(lines).strip()

def my_bookings_text(fullname: str) -> str:
    blocks: List[str] = []
    for cat in CATEGORIES:
        my = []
        for s in state["categories"][cat]["slots"]:
            title = (s.get("title") or "").strip()
            if not title:
                continue
            if fullname in s.get("users", []):
                my.append("• " + title)
        blocks.append(f"🖥 {cat}")
        blocks.extend(my if my else ["—"])
        blocks.append("")
    text = "\n".join(blocks).strip()
    return "Вы никуда не записаны.\n\n" + text if "•" not in text else "Ваши записи:\n\n" + text

# ───────────── known_users (оставим как кэш кто писал) ─────────────
def touch_known_user(uid: int, fullname: str):
    ku = state.setdefault("known_users", {})
    key = str(uid)
    entry = ku.get(key)
    if not isinstance(entry, dict):
        ku[key] = {"name": fullname}
        save_state()
        return
    if entry.get("name") != fullname:
        entry["name"] = fullname
        save_state()

# ───────────── ВЫГРУЗКА УЧАСТНИКОВ ЧЕРЕЗ USER_TOKEN (как в "нормальном" боте) ─────────────
_members_cache: List[Tuple[int, str]] = []
_members_cache_ts: float = 0.0
MEMBERS_CACHE_TTL = 120  # секунд

def fetch_admin_ids_via_user_token() -> List[int]:
    """Берём managers (руководители) через user_api. Это те, кого надо исключать из учеников."""
    if not user_api:
        return []
    ids: List[int] = []
    offset, total = 0, None
    while True:
        data = user_api.groups.getMembers(
            group_id=GROUP_ID,
            filter="managers",
            fields="id",
            count=200,
            offset=offset
        )
        if total is None:
            total = data.get("count", 0)
        items = data.get("items", [])
        for it in items:
            if isinstance(it, dict) and "id" in it:
                ids.append(int(it["id"]))
            elif isinstance(it, int):
                ids.append(int(it))
        offset += len(items)
        if offset >= total or not items:
            break
    return ids

def fetch_members_excluding_admins(force: bool = False) -> List[Tuple[int, str]]:
    """
    Возвращает список [(uid, "Имя Фамилия"), ...] по реальным участникам сообщества,
    исключая админов (managers + локальные ADMINS).
    """
    global _members_cache, _members_cache_ts

    if not user_api:
        # без user_token не можем выгрузить всех подписчиков
        return []

    now = time.time()
    if (not force) and _members_cache and (now - _members_cache_ts) < MEMBERS_CACHE_TTL:
        return _members_cache

    admin_ids = set(fetch_admin_ids_via_user_token()) | set(ADMINS)

    out: List[Tuple[int, str]] = []
    offset, total = 0, None
    while True:
        data = user_api.groups.getMembers(
            group_id=GROUP_ID,
            fields="first_name,last_name,id",
            count=1000,
            offset=offset
        )
        if total is None:
            total = data.get("count", 0)
        items = data.get("items", [])
        for it in items:
            if not isinstance(it, dict):
                continue
            uid = int(it.get("id", 0))
            if uid <= 0:
                continue
            first = it.get("first_name") or ""
            last = it.get("last_name") or ""
            name = f"{first} {last}".strip()
            if not name:
                continue
            if uid in admin_ids:
                continue
            out.append((uid, name))

        offset += len(items)
        if offset >= total or not items:
            break

    # уникализируем по uid
    seen = set()
    uniq = []
    for uid, name in out:
        if uid in seen:
            continue
        seen.add(uid)
        uniq.append((uid, name))

    _members_cache = uniq
    _members_cache_ts = now
    return uniq

def users_get_names(uids: List[int]) -> List[str]:
    if not uids:
        return []
    try:
        api = user_api or session_api
        chunks = [uids[i:i+900] for i in range(0, len(uids), 900)]
        names: List[str] = []
        for chunk in chunks:
            res = api.users.get(user_ids=",".join(map(str, chunk)))
            for u in res:
                names.append(f"{u.get('first_name','')} {u.get('last_name','')}".strip())
        return names
    except Exception:
        return [str(x) for x in uids]

# ───────────── admin commands parsing ─────────────
def _parse_setx_bulk(raw: str):
    parts = raw.strip().split()
    if len(parts) < 1 + 2 + 2:
        return None, None, None, "Формат: /setx.. d1 t1 [d2 t2 ...] CAP LIMIT"
    try:
        capacity = int(parts[-2])
        limit = int(parts[-1])
    except Exception:
        return None, None, None, "Последние два аргумента должны быть числами: CAP LIMIT"
    mid = parts[1:-2]
    if len(mid) % 2 != 0:
        return None, None, None, "Пары дата/время должны идти строго парами."
    pairs = []
    for i in range(0, len(mid), 2):
        d = mid[i].strip()
        t = mid[i+1].strip()
        if d and t:
            pairs.append(f"{d} {t}")
    if not pairs:
        return None, None, None, "Не удалось распознать пары."
    if len(pairs) > 4:
        pairs = pairs[:4]
    return pairs, capacity, limit, None

def _parse_setx_single(raw: str):
    parts = raw.strip().split()
    if len(parts) != 6:
        return None, None, None, None, "Формат: /setx.. N d t CAP LIMIT"
    try:
        n = int(parts[1])
    except Exception:
        return None, None, None, None, "N должен быть числом 1..4."
    if n < 1 or n > 4:
        return None, None, None, None, "N должен быть от 1 до 4."
    d = parts[2].strip()
    t = parts[3].strip()
    if not d or not t:
        return None, None, None, None, "Дата/время не распознаны."
    try:
        cap = int(parts[4])
        lim = int(parts[5])
    except Exception:
        return None, None, None, None, "CAP и LIMIT должны быть числами."
    return n, f"{d} {t}", cap, lim, None

def _ensure_4_slots(cat: str) -> List[dict]:
    cfg = state["categories"][cat]
    slots = cfg.get("slots", [])
    key_to_slot = {s.get("key"): s for s in slots if isinstance(s, dict)}
    fixed = []
    for k in SLOT_KEYS:
        s = key_to_slot.get(k) or {"key": k, "title": "", "users": []}
        s.setdefault("users", [])
        if not isinstance(s["users"], list):
            s["users"] = []
        fixed.append(s)
    cfg["slots"] = fixed
    return fixed

def apply_slots_bulk(cat: str, titles: List[str], capacity: int, limit: int):
    cfg = state["categories"][cat]
    fixed = _ensure_4_slots(cat)
    for i in range(4):
        fixed[i]["title"] = titles[i] if i < len(titles) else ""
    cfg["capacity"] = capacity
    cfg["limit_per_user"] = limit
    save_state()

def apply_slot_single(cat: str, n: int, title: str, capacity: int, limit: int):
    cfg = state["categories"][cat]
    fixed = _ensure_4_slots(cat)
    fixed[n-1]["title"] = title
    cfg["capacity"] = capacity
    cfg["limit_per_user"] = limit
    save_state()

def clear_category(cat: str):
    fixed = _ensure_4_slots(cat)
    for s in fixed:
        s["users"] = []
    save_state()

def delete_slot_no_shift(cat: str, n: int):
    fixed = _ensure_4_slots(cat)
    fixed[n-1]["title"] = ""
    fixed[n-1]["users"] = []
    save_state()

# ───────────── admin edit helpers ─────────────
def category_booked_set(cat: str) -> set:
    booked = set()
    for s in state["categories"][cat]["slots"]:
        booked.update(s.get("users", []))
    return booked

def category_slots_info(cat: str) -> List[Tuple[str, int, int, int, dict]]:
    """[(title, free, taken, cap, slot_dict)] for visible slots"""
    cfg = state["categories"][cat]
    cap = int(cfg.get("capacity", 13))
    out = []
    for s in cfg.get("slots", []):
        title = (s.get("title") or "").strip()
        if not title:
            continue
        users = s.get("users", [])
        taken = len(users)
        free = max(cap - taken, 0)
        out.append((title, free, taken, cap, s))
    return out

def start_admin_edit(user_id: int):
    admin_mode[user_id] = "edit"
    admin_edit[user_id] = {"step": "op"}
    send_msg(user_id, "Редактировать:\nВыберите действие:", kb=admin_edit_keyboard())

def exit_admin_edit(user_id: int, to_panel: bool = True):
    admin_edit.pop(user_id, None)
    admin_mode[user_id] = "panel" if to_panel else ""
    send_msg(user_id, "Ок.", kb=admin_keyboard() if to_panel else None)

def _get_members_names_source() -> List[str]:
    """
    Источник "учеников" для списков.
    1) Если есть USER_TOKEN -> реальные участники groups.getMembers
    2) Иначе -> fallback на known_users (кто писал боту). Без чисток.
    """
    if user_api:
        members = fetch_members_excluding_admins(force=False)
        return sorted([name for (_uid, name) in members], key=lambda s: s.lower())

    # fallback (хуже, но хоть что-то)
    ku = state.get("known_users", {}) or {}
    names = []
    for k, v in ku.items():
        if not str(k).isdigit():
            continue
        uid = int(k)
        if uid in ADMINS:
            continue
        if isinstance(v, dict):
            nm = (v.get("name") or "").strip()
        else:
            nm = str(v).strip()
        if nm:
            names.append(nm)
    return sorted(list(set(names)), key=lambda s: s.lower())

def show_students_list_for_edit(user_id: int):
    st = admin_edit.get(user_id) or {}
    op = st.get("op")
    cat = st.get("cat")
    if op not in {"add", "del"} or cat not in CATEGORIES:
        send_msg(user_id, "Ошибка состояния редактирования. Нажмите «Редактировать» заново.", kb=admin_keyboard())
        admin_edit.pop(user_id, None)
        admin_mode[user_id] = "panel"
        return

    names = _get_members_names_source()
    booked = category_booked_set(cat)

    if op == "add":
        students = [n for n in names if n not in booked]
        header = f"➕ Записать в «{cat}»\nВыберите ученика номером (пишете цифру):"
    else:
        students = [n for n in names if n in booked]
        header = f"🗑 Удалить из «{cat}»\nВыберите ученика номером (пишете цифру):"

    students = sorted(students, key=lambda s: s.lower())
    st["students"] = students
    st["step"] = "pick_student"
    admin_edit[user_id] = st

    if not students:
        send_msg(user_id, "Список пуст.\n(Либо никто не подходит под условие.)", kb=admin_keyboard())
        exit_admin_edit(user_id, to_panel=True)
        return

    MAX_SHOW = 60
    shown = students[:MAX_SHOW]
    body = "\n".join(f"{i+1}. {n}" for i, n in enumerate(shown))
    tail = ""
    if len(students) > MAX_SHOW:
        tail = f"\n\n…и ещё {len(students)-MAX_SHOW} (слишком много). Выберите номер из первых {MAX_SHOW}."

    send_msg(user_id, f"{header}\n\n{body}{tail}\n\nОтмена — кнопка «Отмена» или «Назад».", kb=admin_edit_cat_keyboard())

def show_slots_for_admin_add(user_id: int, cat: str, student_name: str):
    info = category_slots_info(cat)
    if not info:
        send_msg(user_id, f"В «{cat}» нет настроенных слотов. Сначала настройте /setx..", kb=admin_keyboard())
        exit_admin_edit(user_id, to_panel=True)
        return

    st = admin_edit.get(user_id) or {}
    st["step"] = "pick_slot"
    st["student"] = student_name
    admin_edit[user_id] = st

    lines = []
    for i, (t, free, taken, cap, _slot) in enumerate(info, start=1):
        lines.append(f"{i}. {t} | занято: {taken}/{cap} | свободно: {free}")
    send_msg(
        user_id,
        f"Выберите слот номером для «{student_name}» (пишете цифру):\n\n" + "\n".join(lines),
        kb=admin_edit_cat_keyboard()
    )

# ───────────── проверка токена сообщества ─────────────
try:
    gi = session_api.groups.getById(group_id=GROUP_ID)
    print("OK: доступ к группе есть:", gi[0]["name"])
except ApiError as e:
    print("Проблема с доступом к группе:", e)

print("Бот запущен. Нажми Ctrl+C для остановки.")

# ───────────── основной цикл ─────────────
try:
    while True:
        try:
            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW or not event.to_me:
                    continue

                raw = (event.text or "").strip()
                msg = raw
                mlow = raw.lower()
                user_id = event.user_id

                u = session_api.users.get(user_ids=user_id, fields="first_name,last_name")[0]
                fullname = f"{u.get('first_name', '')} {u.get('last_name','')}".strip()

                touch_known_user(user_id, fullname)

                # ───────────── обработка выбора цифрой в админ-редактировании ─────────────
                if user_id in ADMINS and user_id in admin_edit and msg.isdigit():
                    st = admin_edit[user_id]
                    step = st.get("step")

                    # выбор ученика
                    if step == "pick_student":
                        students = st.get("students") or []
                        idx = int(msg) - 1
                        if idx < 0 or idx >= len(students):
                            send_msg(user_id, "Неверный номер. Попробуйте ещё раз.", kb=admin_edit_cat_keyboard())
                            continue

                        chosen = students[idx]
                        op = st.get("op")
                        cat = st.get("cat")

                        if op == "del":
                            removed = remove_user_from_category(chosen, cat)
                            if removed:
                                save_state()
                                send_msg(user_id, f"🗑 Удалено записей: {removed}\n{chosen} — удалён из «{cat}».", kb=admin_keyboard())
                            else:
                                send_msg(user_id, f"У {chosen} нет записей в «{cat}».", kb=admin_keyboard())
                            exit_admin_edit(user_id, to_panel=True)
                            continue

                        # op == add
                        show_slots_for_admin_add(user_id, cat, chosen)
                        continue

                    # выбор слота
                    if step == "pick_slot":
                        cat = st.get("cat")
                        student_name = st.get("student")
                        if not cat or not student_name:
                            send_msg(user_id, "Ошибка состояния. Начните заново.", kb=admin_keyboard())
                            exit_admin_edit(user_id, to_panel=True)
                            continue

                        info = category_slots_info(cat)
                        idx = int(msg) - 1
                        if idx < 0 or idx >= len(info):
                            send_msg(user_id, "Неверный номер слота. Попробуйте ещё раз.", kb=admin_edit_cat_keyboard())
                            continue

                        title, free, taken, cap, slot = info[idx]
                        cfg = state["categories"][cat]
                        lim = int(cfg.get("limit_per_user", 1))

                        if count_user_bookings_in_category(student_name, cat) >= lim:
                            send_msg(user_id, f"У {student_name} уже есть запись в «{cat}». Сначала удалите.", kb=admin_keyboard())
                            exit_admin_edit(user_id, to_panel=True)
                            continue

                        if len(slot.get("users", [])) >= cap:
                            send_msg(user_id, f"Слот переполнен ({cap}). Выберите другой слот.", kb=admin_edit_cat_keyboard())
                            continue

                        slot["users"].append(student_name)
                        save_state()
                        send_msg(user_id, f"✅ Записан: {student_name}\n{cat} → {title}", kb=admin_keyboard())
                        exit_admin_edit(user_id, to_panel=True)
                        continue

                # ───────────── ГЛОБАЛЬНО: "Назад" / "Отмена" ─────────────
                if msg == "Отмена":
                    pending_cat.pop(user_id, None)
                    pending_rewrite.pop(user_id, None)
                    if user_id in admin_edit:
                        exit_admin_edit(user_id, to_panel=True)
                        continue
                    send_msg(user_id, "Ок, отменено.")
                    continue

                if msg == "Назад":
                    if admin_mode.get(user_id) == "edit":
                        admin_edit.pop(user_id, None)
                        admin_mode[user_id] = "panel"
                        send_msg(user_id, "Панель администратора:", kb=admin_keyboard())
                        continue
                    if admin_mode.get(user_id) == "panel":
                        admin_mode[user_id] = ""
                        send_msg(user_id, "Ок.")
                        continue
                    if pending_rewrite.get(user_id) == "menu":
                        pending_rewrite.pop(user_id, None)
                        send_msg(user_id, "Ок.")
                        continue
                    send_msg(user_id, "Ок.")
                    continue

                # ───────────── админ-команды текстом ─────────────
                if user_id in ADMINS:
                    if mlow == CMD_CLEAR_PR:
                        clear_category(CAT_PR)
                        send_msg(user_id, "✅ Очищено: Программирование (все записи удалены).")
                        continue
                    if mlow == CMD_CLEAR_BH:
                        clear_category(CAT_BH)
                        send_msg(user_id, "✅ Очищено: Бухгалтерия (все записи удалены).")
                        continue

                    if mlow.startswith(CMD_DEL_PR) or mlow.startswith(CMD_DEL_BH):
                        parts = raw.strip().split()
                        if len(parts) != 2 or not parts[1].isdigit():
                            send_msg(user_id, "Формат: /delpr N  или  /delbh N (N=1..4)")
                            continue
                        n = int(parts[1])
                        if n < 1 or n > 4:
                            send_msg(user_id, "N должен быть от 1 до 4.")
                            continue
                        cat = CAT_PR if mlow.startswith(CMD_DEL_PR) else CAT_BH
                        delete_slot_no_shift(cat, n)
                        send_msg(user_id, f"✅ Удалён слот {n} в «{cat}» (без сдвига).")
                        continue

                    if mlow.startswith(CMD_SET_PR) or mlow.startswith(CMD_SET_BH):
                        cat = CAT_PR if mlow.startswith(CMD_SET_PR) else CAT_BH

                        n, title, cap, lim, err_single = _parse_setx_single(raw)
                        if err_single is None:
                            apply_slot_single(cat, n, title, cap, lim)
                            send_msg(user_id, f"✅ Обновлён слот {n} в «{cat}»: {title}\nCAP={cap}, LIMIT={lim}")
                            continue

                        titles, cap2, lim2, err_bulk = _parse_setx_bulk(raw)
                        if err_bulk:
                            send_msg(
                                user_id,
                                "⚠️ " + err_bulk + "\n\nПримеры:\n"
                                "/setxpr 1 19.01 18:00-20:00 12 1\n"
                                "/setxbh 4 22.01 18:00-20:00 12 1\n"
                                "/setxpr 19.01 18:00-20:00 20.01 18:00-20:00 12 1"
                            )
                            continue
                        apply_slots_bulk(cat, titles or [], cap2 or 13, lim2 or 1)
                        send_msg(user_id, f"✅ Обновлено расписание «{cat}» (без сброса записей).")
                        continue

                # ───────────── меню ─────────────
                if mlow in {"старт", "start", "привет", "меню"}:
                    pending_rewrite.pop(user_id, None)
                    pending_cat.pop(user_id, None)
                    admin_edit.pop(user_id, None)
                    admin_mode[user_id] = ""
                    send_msg(user_id, "Выберите действие:")
                    continue

                if msg == "Инструкция":
                    send_msg(
                        user_id,
                        "🧾 Инструкция\n\n"
                        "• «Выбрать» → выберите направление, затем слот.\n"
                        "• «Перезапись» → сбросить одну категорию или всё.\n"
                        "• «Расписание» → кратко, затем «Подробно».\n"
                        "• «Мои записи» → ваши записи.\n"
                    )
                    continue

                if msg == "Расписание":
                    send_msg(user_id, schedule_summary_text(), kb=schedule_keyboard())
                    continue

                if msg == "Подробно":
                    send_msg(user_id, schedule_detailed_text(), kb=schedule_keyboard())
                    continue

                if msg == "Мои записи":
                    send_msg(user_id, my_bookings_text(fullname))
                    continue

                # Перезапись
                if msg == "Перезапись":
                    pending_rewrite[user_id] = "menu"
                    send_msg(user_id, "Что сбросить?", kb=rewrite_keyboard())
                    continue

                if pending_rewrite.get(user_id) == "menu":
                    if msg == "Перезапись: Программирование":
                        removed = remove_user_from_category(fullname, CAT_PR)
                        if removed:
                            save_state()
                            send_msg(user_id, "✅ Сброшено: Программирование. Теперь выберите слот заново.")
                        else:
                            send_msg(user_id, "У вас нет записей в Программировании.")
                        pending_rewrite.pop(user_id, None)
                        continue

                    if msg == "Перезапись: Бухгалтерия":
                        removed = remove_user_from_category(fullname, CAT_BH)
                        if removed:
                            save_state()
                            send_msg(user_id, "✅ Сброшено: Бухгалтерия. Теперь выберите слот заново.")
                        else:
                            send_msg(user_id, "У вас нет записей в Бухгалтерии.")
                        pending_rewrite.pop(user_id, None)
                        continue

                    if msg == "Перезапись: Всё":
                        removed = remove_user_from_all_categories(fullname)
                        if removed:
                            save_state()
                            send_msg(user_id, "✅ Ваши записи очищены. Теперь выберите слоты заново.")
                        else:
                            send_msg(user_id, "У вас нет активных записей.")
                        pending_rewrite.pop(user_id, None)
                        continue

                # ───────────── админ-панель ─────────────
                if msg == "Админам":
                    if user_id not in ADMINS:
                        send_msg(user_id, "🚫 Вы не администратор.")
                        continue
                    admin_mode[user_id] = "panel"
                    admin_edit.pop(user_id, None)
                    send_msg(user_id, "Панель администратора:", kb=admin_keyboard())
                    continue

                if msg == "Редактировать":
                    if user_id not in ADMINS:
                        send_msg(user_id, "🚫 Вы не администратор.")
                        continue
                    start_admin_edit(user_id)
                    continue

                if user_id in ADMINS and admin_mode.get(user_id) == "edit":
                    if msg == "Записать":
                        admin_edit[user_id] = {"step": "cat", "op": "add"}
                        send_msg(user_id, "Куда записать? Выберите предмет:", kb=admin_edit_cat_keyboard())
                        continue
                    if msg == "Удалить":
                        admin_edit[user_id] = {"step": "cat", "op": "del"}
                        send_msg(user_id, "Откуда удалить? Выберите предмет:", kb=admin_edit_cat_keyboard())
                        continue

                    st = admin_edit.get(user_id) or {}
                    if st.get("step") == "cat" and msg in {CAT_PR, CAT_BH}:
                        st["cat"] = msg
                        admin_edit[user_id] = st
                        show_students_list_for_edit(user_id)
                        continue

                if msg == "Инструкция (админ)":
                    if user_id not in ADMINS:
                        send_msg(user_id, "🚫 Вы не администратор.")
                        continue
                    text = (
                        "🛠 Инструкция для админа\n\n"
                        "Точечная настройка слота:\n"
                        "• /setxpr N ДАТА ВРЕМЯ CAP LIMIT\n"
                        "  пример: /setxpr 1 19.01 18:00-20:00 12 1\n"
                        "• /setxbh N ДАТА ВРЕМЯ CAP LIMIT\n"
                        "  пример: /setxbh 4 22.01 18:00-20:00 12 1\n\n"
                        "Массовая настройка (до 4 слотов):\n"
                        "• /setxpr d1 t1 [d2 t2 ...] CAP LIMIT\n"
                        "  пример: /setxpr 19.01 18:00-20:00 20.01 18:00-20:00 12 1\n"
                        "• /setxbh d1 t1 [d2 t2 ...] CAP LIMIT\n\n"
                        "Удаление слота БЕЗ сдвига:\n"
                        "• /delpr N  — очистит только слот N в Программировании\n"
                        "• /delbh N  — очистит только слот N в Бухгалтерии\n\n"
                        "Полная очистка категории:\n"
                        "• /clearpr\n"
                        "• /clearbh\n\n"
                        "Редактирование через кнопки:\n"
                        "Админам → Редактировать → Записать/Удалить → Предмет → номер ученика → (для записи) номер слота"
                    )
                    send_msg(user_id, text, kb=admin_keyboard())
                    continue

                if msg == "Админы":
                    if user_id not in ADMINS:
                        send_msg(user_id, "🚫 Вы не администратор.")
                        continue
                    ids_all = sorted(set([i for i in ADMINS if isinstance(i, int)]))
                    names = users_get_names(ids_all)
                    body = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names)) or "—"
                    send_msg(user_id, f"🛡 Администраторы ({len(ids_all)}):\n{body}", kb=admin_keyboard())
                    continue

                if msg == "Ученики":
                    if user_id not in ADMINS:
                        send_msg(user_id, "🚫 Вы не администратор.")
                        continue

                    if not user_api:
                        # fallback без user_token
                        names = _get_members_names_source()
                        if not names:
                            send_msg(user_id, "👥 Ученики: — (нет USER_TOKEN и кэш пуст).", kb=admin_keyboard())
                        else:
                            body = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
                            send_msg(user_id, f"👥 Ученики ({len(names)}):\n{body}\n\n⚠️ Без USER_TOKEN список может быть неполным.", kb=admin_keyboard())
                        continue

                    try:
                        members = fetch_members_excluding_admins(force=True)
                        names = sorted([name for (_uid, name) in members], key=lambda s: s.lower())
                        body = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names)) or "—"
                        send_msg(user_id, f"👥 Ученики ({len(names)}):\n{body}", kb=admin_keyboard())
                    except Exception as e:
                        send_msg(user_id, f"⚠️ Не удалось получить список учеников: {e}", kb=admin_keyboard())
                    continue

                if msg == "Незаписавшиеся ученики":
                    if user_id not in ADMINS:
                        send_msg(user_id, "🚫 Вы не администратор.")
                        continue

                    names = _get_members_names_source()
                    if not names:
                        send_msg(user_id, "📋 Незаписавшиеся: — (нет данных о подписчиках).", kb=admin_keyboard())
                        continue

                    booked_pr = category_booked_set(CAT_PR)
                    booked_bh = category_booked_set(CAT_BH)

                    lines = []
                    for n in names:
                        missing = []
                        if n not in booked_pr:
                            missing.append(CAT_PR)
                        if n not in booked_bh:
                            missing.append(CAT_BH)
                        if missing:
                            lines.append(f"• {n} — не записан(а): {', '.join(missing)}")

                    if not lines:
                        send_msg(user_id, "📋 Незаписавшиеся ученики: нет.", kb=admin_keyboard())
                    else:
                        send_msg(user_id, f"📋 Незаписавшиеся ученики ({len(lines)}):\n\n" + "\n".join(lines), kb=admin_keyboard())
                    continue

                # ───────────── выбор направления/слота для ученика ─────────────
                if msg == "Выбрать":
                    pending_cat.pop(user_id, None)
                    send_msg(user_id, "Выберите направление:", kb=choose_category_keyboard())
                    continue

                if msg in {CAT_PR, CAT_BH}:
                    pending_cat[user_id] = msg
                    visible_titles = [
                        (s.get("title") or "").strip()
                        for s in state["categories"][msg]["slots"]
                        if (s.get("title") or "").strip()
                    ]
                    if not visible_titles:
                        send_msg(user_id, "⚠️ Слоты пока не настроены администратором.")
                        pending_cat.pop(user_id, None)
                        continue
                    send_msg(user_id, f"{msg}. Выберите слот:", kb=slots_keyboard(msg))
                    continue

                if user_id in pending_cat:
                    cat = pending_cat[user_id]
                    slots_list = state["categories"][cat]["slots"]
                    titles = [(s.get("title") or "").strip() for s in slots_list if (s.get("title") or "").strip()]
                    if msg in titles:
                        cfg = state["categories"][cat]
                        cap = int(cfg.get("capacity", 13))
                        lim = int(cfg.get("limit_per_user", 1))

                        slot = next((s for s in slots_list if (s.get("title") or "").strip() == msg), None)
                        if slot is None:
                            send_msg(user_id, "Не удалось определить слот.")
                            continue

                        if fullname in slot["users"]:
                            send_msg(user_id, "Вы уже записаны на этот слот.")
                            continue

                        if count_user_bookings_in_category(fullname, cat) >= lim:
                            send_msg(user_id, f"У вас уже есть запись в категории «{cat}».")
                            continue

                        if len(slot["users"]) >= cap:
                            send_msg(user_id, f"Слот переполнен ({cap}).")
                            continue

                        slot["users"].append(fullname)
                        save_state()
                        pending_cat.pop(user_id, None)
                        send_msg(user_id, f"✅ Записаны: {cat} → {slot['title']}")
                        continue

                send_msg(user_id, "Не понял команду. Выберите действие:")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"⚠️ Сетевая ошибка: {e}. Повтор через 5 сек...")
            time.sleep(5)

except KeyboardInterrupt:
    print("\n🛑 Бот остановлен пользователем (Ctrl+C). До встречи!")
