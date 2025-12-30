# -*- coding: utf-8 -*-
# VK-bot расписания (2 категории) + Render health-check + GitHub Gist persistence
# Вариант B: "Ученики" = кэш известных пользователей, но показываем ТОЛЬКО тех,
# кто сейчас состоит/подписан на сообщество (проверка через groups.isMember).
# Отписался -> удаляем из кэша и (дополнительно) снимаем с записей.
#
# Слоты в каждой категории имеют стабильные ключи S1..S4.
# /setxpr и /setxbh меняют ТОЛЬКО title слотов, users НЕ трогаем.
# Очистка записей админом: /clearpr /clearbh
#
# НОВОЕ: Перезапись -> подменю (Программирование / Бухгалтерия / Всё)

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
USER_TOKEN = os.getenv("USER_TOKEN")         # опционально
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
    print("USER_TOKEN не указан (это нормально).")

# ───────────── категории ─────────────
CAT_PR = "Программирование"
CAT_BH = "Бухгалтерия"
CATEGORIES = [CAT_PR, CAT_BH]

CMD_SET_PR = "/setxpr"
CMD_SET_BH = "/setxbh"
CMD_CLEAR_PR = "/clearpr"
CMD_CLEAR_BH = "/clearbh"

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
        "known_users": {},  # "uid": {"name": "Имя Фамилия"}
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
            if k in key_to_slot:
                new_slots.append(key_to_slot[k])
            else:
                new_slots.append({"key": k, "title": "", "users": []})
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
ADMINS: List[int] = [aid for aid in [MASTER_ID, 1080975674] if isinstance(aid, int)]

# ───────────── runtime ─────────────
pending_cat: Dict[int, str] = {}
pending_action: Dict[int, Dict] = {}
pending_rewrite: Dict[int, str] = {}  # user_id -> "menu" ожидание выбора что сбросить

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

def rewrite_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Перезапись: Программирование", VkKeyboardColor.PRIMARY)
    kb.add_button("Перезапись: Бухгалтерия", VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Перезапись: Всё", VkKeyboardColor.NEGATIVE)
    kb.add_button("Назад", VkKeyboardColor.SECONDARY)
    return kb

def schedule_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Подробно", VkKeyboardColor.PRIMARY)
    kb.add_button("Назад", VkKeyboardColor.SECONDARY)
    return kb

def admin_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Ученики", VkKeyboardColor.SECONDARY)
    kb.add_button("Админы", VkKeyboardColor.SECONDARY)
    kb.add_button("Незаписавшиеся ученики", VkKeyboardColor.SECONDARY)
    kb.add_line()
    kb.add_button("Редактировать", VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Назад", VkKeyboardColor.NEGATIVE)
    return kb

def edit_keyboard() -> VkKeyboard:
    kb = VkKeyboard(one_time=False)
    kb.add_button("Записать ученика", VkKeyboardColor.POSITIVE)
    kb.add_button("Удалить ученика", VkKeyboardColor.NEGATIVE)
    kb.add_line()
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

# ───────────── helpers ─────────────
def send_msg(user_id: int, text: str, kb: Optional[VkKeyboard] = None):
    payload = {"user_id": user_id, "message": text, "random_id": 0}
    payload["keyboard"] = (kb or base_keyboard(user_id in ADMINS)).get_keyboard()
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

def schedule_summary_text() -> str:
    lines: List[str] = ["📅 Расписание (кратко)\n"]
    for cat in CATEGORIES:
        cfg = state["categories"][cat]
        cap = int(cfg.get("capacity", 13))
        slots = cfg.get("slots", [])
        lines.append(f"🖥 {cat}")
        any_visible = False
        for s in slots:
            title = (s.get("title") or "").strip()
            if not title:
                continue
            any_visible = True
            taken = len(s["users"])
            free = max(cap - taken, 0)
            lines.append(f"{title} | занято: {taken}/{cap} | свободно: {free}")
        if not any_visible:
            lines.append("Слоты не настроены администратором.\n")
        lines.append("")
    lines.append("Нажмите «Подробно», чтобы увидеть списки записанных.")
    return "\n".join(lines).strip()

def schedule_detailed_text() -> str:
    lines: List[str] = ["📅 Расписание (подробно)\n"]
    for cat in CATEGORIES:
        cfg = state["categories"][cat]
        cap = int(cfg.get("capacity", 13))
        slots = cfg.get("slots", [])
        lines.append(f"🖥 {cat}")
        any_visible = False
        for s in slots:
            title = (s.get("title") or "").strip()
            if not title:
                continue
            any_visible = True
            users = s["users"]
            taken = len(users)
            free = max(cap - taken, 0)
            lines.append(f"{title} | занято: {taken}/{cap} | свободно: {free}\n")
            lines.append(roster_with_numbers(users))
            lines.append("")
        if not any_visible:
            lines.append("Слоты не настроены администратором.\n")
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
            if fullname in s["users"]:
                my.append("• " + title)
        blocks.append(f"🖥 {cat}")
        blocks.extend(my if my else ["—"])
        blocks.append("")
    text = "\n".join(blocks).strip()
    return "Вы никуда не записаны.\n\n" + text if "•" not in text else "Ваши записи:\n\n" + text

# ───────────── known_users + isMember ─────────────
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

def _groups_is_member_batch(user_ids: List[int]) -> Dict[int, bool]:
    if not user_ids:
        return {}
    CHUNK = 500
    out: Dict[int, bool] = {}

    def call(api):
        for i in range(0, len(user_ids), CHUNK):
            chunk = user_ids[i:i+CHUNK]
            res = api.groups.isMember(group_id=GROUP_ID, user_ids=",".join(map(str, chunk)))
            if isinstance(res, list):
                for it in res:
                    uid = int(it.get("user_id", 0))
                    out[uid] = bool(it.get("member", 0))

    try:
        call(session_api)
        return out
    except Exception as e1:
        if user_api:
            try:
                out.clear()
                call(user_api)
                return out
            except Exception as e2:
                print("groups.isMember failed:", e1, "| fallback failed:", e2)
        else:
            print("groups.isMember failed:", e1)

    return {uid: True for uid in user_ids}

def prune_known_users_and_bookings() -> Tuple[List[str], int]:
    ku = state.get("known_users", {}) or {}
    ids: List[int] = [int(k) for k in ku.keys() if str(k).isdigit()]
    membership = _groups_is_member_batch(ids)

    removed_count = 0
    active_names: List[str] = []
    to_remove: List[Tuple[str, str]] = []

    for uid in ids:
        uid_str = str(uid)
        entry = ku.get(uid_str, {})
        name = entry.get("name", "").strip() if isinstance(entry, dict) else ""
        is_member = membership.get(uid, True)

        if not is_member:
            to_remove.append((uid_str, name))
        else:
            if uid not in ADMINS and name:
                active_names.append(name)

    if to_remove:
        for uid_str, name in to_remove:
            ku.pop(uid_str, None)
            removed_count += 1
            if name:
                remove_user_from_all_categories(name)
        state["known_users"] = ku
        save_state()

    active_names = sorted(set(active_names), key=lambda s: s.lower())
    return active_names, removed_count

# ───────────── setx / apply_slots / clear_category ─────────────
def parse_setx_command(raw: str):
    parts = raw.strip().split()
    if len(parts) < 1 + 2 + 2:
        return None, None, None, "Формат: /setx.. d1 t1 [d2 t2 ...] CAPACITY LIMIT"
    try:
        capacity = int(parts[-2])
        limit = int(parts[-1])
    except Exception:
        return None, None, None, "Последние два аргумента должны быть числами: CAPACITY LIMIT"
    if capacity <= 0 or capacity > 500:
        return None, None, None, "CAPACITY должен быть от 1 до 500."
    if limit <= 0 or limit > 10:
        return None, None, None, "LIMIT должен быть от 1 до 10."
    mid = parts[1:-2]
    if len(mid) % 2 != 0:
        return None, None, None, "Пары дата/время должны идти строго парами: d1 t1 d2 t2 ..."
    pairs = []
    for i in range(0, len(mid), 2):
        d = mid[i].strip()
        t = mid[i+1].strip()
        if d and t:
            pairs.append(f"{d} {t}")
    if not pairs:
        return None, None, None, "Не удалось распознать пары (дата время)."
    if len(pairs) > 4:
        pairs = pairs[:4]
    return pairs, capacity, limit, None

def apply_slots(cat: str, titles: List[str], capacity: int, limit: int):
    cfg = state["categories"][cat]
    slots = cfg.get("slots", [])
    key_to_slot = {s.get("key"): s for s in slots if isinstance(s, dict)}
    new_slots = []
    for k in SLOT_KEYS:
        s = key_to_slot.get(k)
        if not isinstance(s, dict):
            s = {"key": k, "title": "", "users": []}
        s.setdefault("users", [])
        if not isinstance(s["users"], list):
            s["users"] = []
        new_slots.append(s)
    for i, k in enumerate(SLOT_KEYS):
        new_slots[i]["title"] = titles[i] if i < len(titles) else ""
    cfg["capacity"] = capacity
    cfg["limit_per_user"] = limit
    cfg["slots"] = new_slots
    save_state()

def clear_category(cat: str):
    cfg = state["categories"][cat]
    for s in cfg.get("slots", []):
        if isinstance(s, dict):
            s["users"] = []
    save_state()

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

                # админ очистка
                if mlow == CMD_CLEAR_PR and user_id in ADMINS:
                    clear_category(CAT_PR)
                    send_msg(user_id, "✅ Очищено: Программирование (все записи удалены).")
                    continue
                if mlow == CMD_CLEAR_BH and user_id in ADMINS:
                    clear_category(CAT_BH)
                    send_msg(user_id, "✅ Очищено: Бухгалтерия (все записи удалены).")
                    continue

                # админ setx
                if (mlow.startswith(CMD_SET_PR) or mlow.startswith(CMD_SET_BH)) and (user_id in ADMINS):
                    cat = CAT_PR if mlow.startswith(CMD_SET_PR) else CAT_BH
                    titles, cap, lim, err = parse_setx_command(raw)
                    if err:
                        send_msg(user_id, "⚠️ " + err)
                        continue
                    apply_slots(cat, titles or [], cap or 13, lim or 1)
                    send_msg(
                        user_id,
                        f"✅ Настроено: {cat}\n"
                        f"Слотов: {len(titles)} | Мест: {cap} | Лимит на ученика в категории: {lim}\n\n"
                        + "\n".join(f"• {t}" for t in titles),
                    )
                    continue

                # меню
                if mlow in {"старт", "start", "привет", "меню"}:
                    pending_rewrite.pop(user_id, None)
                    send_msg(user_id, "Выберите действие:")
                    continue

                if msg == "Инструкция":
                    send_msg(
                        user_id,
                        "🧾 Инструкция\n\n"
                        "• «Выбрать» → выберите направление, затем слот.\n"
                        "• «Перезапись» → теперь можно сбросить одну категорию или всё.\n"
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

                # ── Перезапись: подменю ──
                if msg == "Перезапись":
                    pending_rewrite[user_id] = "menu"
                    send_msg(user_id, "Что сбросить?", kb=rewrite_keyboard())
                    continue

                if pending_rewrite.get(user_id) == "menu":
                    if msg == "Перезапись: Программирование":
                        removed = remove_user_from_category(fullname, CAT_PR)
                        if removed:
                            save_state()
                            send_msg(user_id, "✅ Сброшено: Программирование. Теперь можете выбрать слот заново.")
                        else:
                            send_msg(user_id, "У вас нет записей в Программировании.")
                        pending_rewrite.pop(user_id, None)
                        continue

                    if msg == "Перезапись: Бухгалтерия":
                        removed = remove_user_from_category(fullname, CAT_BH)
                        if removed:
                            save_state()
                            send_msg(user_id, "✅ Сброшено: Бухгалтерия. Теперь можете выбрать слот заново.")
                        else:
                            send_msg(user_id, "У вас нет записей в Бухгалтерии.")
                        pending_rewrite.pop(user_id, None)
                        continue

                    if msg == "Перезапись: Всё":
                        removed = remove_user_from_all_categories(fullname)
                        if removed:
                            save_state()
                            send_msg(user_id, "✅ Ваши записи очищены. Теперь можете выбрать слоты заново.")
                        else:
                            send_msg(user_id, "У вас нет активных записей.")
                        pending_rewrite.pop(user_id, None)
                        continue

                    if msg == "Назад":
                        pending_rewrite.pop(user_id, None)
                        send_msg(user_id, "Ок.")
                        continue

                # выбор
                if msg == "Выбрать":
                    pending_cat.pop(user_id, None)
                    send_msg(user_id, "Выберите направление:", kb=choose_category_keyboard())
                    continue

                if msg in {CAT_PR, CAT_BH}:
                    pending_cat[user_id] = msg
                    visible_titles = [(s.get("title") or "").strip() for s in state["categories"][msg]["slots"] if (s.get("title") or "").strip()]
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

                if msg == "Отмена":
                    pending_cat.pop(user_id, None)
                    pending_action.pop(user_id, None)
                    pending_rewrite.pop(user_id, None)
                    send_msg(user_id, "Ок, отменено.")
                    continue

                # Админка (оставил только ключевые кнопки, как было)
                if msg == "Админам":
                    if user_id not in ADMINS:
                        send_msg(user_id, "🚫 Вы не администратор.")
                        continue
                    send_msg(user_id, "Панель администратора:", kb=admin_keyboard())
                    continue

                if msg == "Ученики" and user_id in ADMINS:
                    names, removed = prune_known_users_and_bookings()
                    if not names:
                        send_msg(user_id, "👥 Ученики: —")
                    else:
                        body = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
                        extra = f"\n\n(Удалено из кэша: {removed})" if removed else ""
                        send_msg(user_id, f"👥 Ученики ({len(names)}):\n{body}{extra}")
                    continue

                if msg == "Незаписавшиеся ученики" and user_id in ADMINS:
                    names, removed = prune_known_users_and_bookings()
                    if not names:
                        send_msg(user_id, "📋 Незаписавшиеся: —")
                        continue
                    booked_pr = set()
                    booked_bh = set()
                    for s in state["categories"][CAT_PR]["slots"]:
                        booked_pr.update(s["users"])
                    for s in state["categories"][CAT_BH]["slots"]:
                        booked_bh.update(s["users"])
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
                        send_msg(user_id, "📋 Незаписавшиеся ученики: нет.")
                    else:
                        extra = f"\n\n(Удалено из кэша: {removed})" if removed else ""
                        send_msg(user_id, f"📋 Незаписавшиеся ученики ({len(lines)}):\n\n" + "\n".join(lines) + extra)
                    continue

                send_msg(user_id, "Не понял команду. Выберите действие:")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"⚠️ Сетевая ошибка: {e}. Повтор через 5 сек...")
            time.sleep(5)

except KeyboardInterrupt:
    print("\n🛑 Бот остановлен пользователем (Ctrl+C). До встречи!")
