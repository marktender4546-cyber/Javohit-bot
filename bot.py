"""
Onlayn kurslar katalogi — Telegram bot (aiogram 3), Render uchun.

* Kurslar courses.json faylidan olinadi (rasmlar images/ papkasida yoki URL)
* Ariza (lead) adminlarga yuboriladi
* /admin — statistika, arizalar, CSV, ommaviy xabar
* Render'da webhook, lokal ishga tushirishda polling
"""
import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from i18n import LANGS, t

# ----------------------------------------------------------------- sozlamalar
BASE_DIR = Path(__file__).parent
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ADMIN_IDS", ""))}
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "bot.db"))
COURSES_FILE = Path(os.getenv("COURSES_FILE", str(BASE_DIR / "courses.json")))
IMAGES_DIR = BASE_DIR / "images"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:64]
PAGE_SIZE = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("coursebot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ----------------------------------------------------------------- katalog
DATA = json.loads(COURSES_FILE.read_text(encoding="utf-8"))
SET = DATA["settings"]
CATS = {c["id"]: c for c in DATA["categories"]}
COURSES = {c["id"]: c for c in DATA["courses"]}


def L(obj, lang: str) -> str:
    """{'uz': .., 'ru': ..} ko'rinishidagi maydondan kerakli tilni oladi."""
    if isinstance(obj, dict):
        return obj.get(lang) or obj.get("uz") or next(iter(obj.values()), "")
    return obj or ""


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def money(v, lang: str) -> str:
    return f"{int(v):,}".replace(",", " ") + " " + L(SET.get("currency", "so‘m"), lang)


def photo_of(ref: str | None):
    """URL, images/ papkasidagi fayl yoki Telegram file_id."""
    if not ref:
        return None
    if ref.startswith("http"):
        return ref
    p = IMAGES_DIR / ref
    return FSInputFile(p) if p.exists() else ref


# ----------------------------------------------------------------- baza
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, lang TEXT, name TEXT, username TEXT,
        joined INTEGER, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS favs(uid INTEGER, cid TEXT, PRIMARY KEY(uid, cid));
    CREATE TABLE IF NOT EXISTS leads(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, username TEXT, course TEXT,
        name TEXT, phone TEXT, comment TEXT, ts INTEGER);
    """
)
db.commit()


def get_user(tg_user) -> sqlite3.Row:
    row = db.execute("SELECT * FROM users WHERE id=?", (tg_user.id,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users(id, lang, name, username, joined) VALUES(?,?,?,?,?)",
            (tg_user.id, None, tg_user.full_name, tg_user.username, int(time.time())),
        )
        db.commit()
        row = db.execute("SELECT * FROM users WHERE id=?", (tg_user.id,)).fetchone()
    elif not row["active"]:
        db.execute("UPDATE users SET active=1 WHERE id=?", (tg_user.id,))
        db.commit()
    return row


def lang_of(tg_user) -> str:
    return get_user(tg_user)["lang"] or "uz"


def is_fav(uid: int, cid: str) -> bool:
    return db.execute("SELECT 1 FROM favs WHERE uid=? AND cid=?", (uid, cid)).fetchone() is not None


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ----------------------------------------------------------------- tugmalar / ko'rinishlar
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


# View = (matn, klaviatura, rasm yoki None)
def v_home(lang: str, name: str | None = None):
    text = (
        t(lang, "welcome", name=esc(name), brand=esc(SET.get("brand", "")))
        if name
        else t(lang, "menu_hint")
    )
    rows = [
        [btn(t(lang, "m_courses"), "cats"), btn(t(lang, "m_search"), "search")],
        [btn(t(lang, "m_favs"), "favs"), btn(t(lang, "m_apply"), "apply:0")],
        [btn(t(lang, "m_faq"), "faq"), btn(t(lang, "m_about"), "about")],
        [btn(t(lang, "m_contact"), "contact"), btn(t(lang, "m_lang"), "lang")],
    ]
    return text, kb(rows), None


def v_cats(lang: str):
    rows = [[btn(L(c["title"], lang), f"cat:{cid}:0")] for cid, c in CATS.items()]
    rows.append([btn(t(lang, "all_courses"), "cat:all:0")])
    rows.append([btn(t(lang, "home"), "home")])
    return t(lang, "cats_title"), kb(rows), None


def course_button(c: dict, lang: str) -> list:
    label = f"{L(c['title'], lang)} · {money(c['price'], lang)}" if c.get("price") else L(c["title"], lang)
    return [btn(clip(label, 60), f"c:{c['id']}:info")]


def v_list(lang: str, cat_id: str, page: int):
    items = [c for c in COURSES.values() if cat_id == "all" or c["category"] == cat_id]
    title = t(lang, "all_courses") if cat_id == "all" else L(CATS.get(cat_id, {}).get("title"), lang)
    if not items:
        return t(lang, "empty"), kb([[btn(t(lang, "back"), "cats")]]), None
    pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    rows = [course_button(c, lang) for c in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]]
    if pages > 1:
        rows.append(
            [
                btn("◀️", f"cat:{cat_id}:{page - 1}") if page > 0 else btn("·", "noop"),
                btn(f"{page + 1}/{pages}", "noop"),
                btn("▶️", f"cat:{cat_id}:{page + 1}") if page < pages - 1 else btn("·", "noop"),
            ]
        )
    rows.append([btn(t(lang, "back"), "cats"), btn(t(lang, "home"), "home")])
    return t(lang, "list_title", cat=f"<b>{esc(title)}</b>"), kb(rows), None


def card_lines(c: dict, lang: str, tab: str) -> list[str]:
    lines = [f"<b>{esc(L(c['title'], lang))}</b>"]
    if tab == "info":
        desc = L(c.get("description") or c.get("short"), lang)
        lines += ["", esc(clip(desc, 380)), ""]
        price = c.get("price", 0)
        if not price:
            lines.append(f"💰 {t(lang, 'price')}: <b>{t(lang, 'free')}</b>")
        elif c.get("old_price") and c["old_price"] > price:
            pct = round((1 - price / c["old_price"]) * 100)
            lines.append(
                f"💰 {t(lang, 'price')}: <s>{money(c['old_price'], lang)}</s> → <b>{money(price, lang)}</b> (-{pct}%)"
            )
        else:
            lines.append(f"💰 {t(lang, 'price')}: <b>{money(price, lang)}</b>")
        if c.get("duration"):
            lines.append(f"⏱ {t(lang, 'duration')}: {esc(L(c['duration'], lang))}")
        if c.get("lessons"):
            lines.append(f"🎞 {t(lang, 'lessons')}: {c['lessons']}")
        if c.get("level"):
            lines.append(f"📊 {t(lang, 'level')}: {esc(L(c['level'], lang))}")
        if c.get("format"):
            lines.append(f"💻 {t(lang, 'format')}: {esc(L(c['format'], lang))}")
    elif tab == "program":
        lines += ["", t(lang, "program_title"), ""]
        for i, item in enumerate((c.get("program") or {}).get(lang) or (c.get("program") or {}).get("uz", []), 1):
            lines.append(f"{i}. {esc(clip(item, 70))}")
    elif tab == "teacher":
        tc = c.get("teacher") or {}
        lines += ["", t(lang, "teacher_title"), "", f"<b>{esc(tc.get('name'))}</b>", esc(clip(L(tc.get("bio"), lang), 450))]
    elif tab == "reviews":
        lines += ["", t(lang, "reviews_title"), ""]
        for r in (c.get("reviews") or [])[:4]:
            lines.append(f"{'⭐' * int(r.get('rating', 5))} <b>{esc(r.get('name'))}</b>")
            lines.append(f"<i>{esc(clip(L(r.get('text'), lang), 160))}</i>")
            lines.append("")
    return lines


def fit(lines: list[str], limit: int = 1000) -> str:
    """Sarlavhali yozuvni 1024 belgilik caption chegarasiga sig'diradi (butun qatorlar bilan)."""
    out, total = [], 0
    for line in lines:
        if total + len(line) + 1 > limit and out:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out).rstrip()


def v_card(lang: str, uid: int, cid: str, tab: str = "info"):
    c = COURSES[cid]
    tabs = [("info", True), ("program", bool(c.get("program"))), ("teacher", bool(c.get("teacher"))), ("reviews", bool(c.get("reviews")))]
    tab_btns = [
        btn(("• " if k == tab else "") + t(lang, f"tab_{k}"), f"c:{cid}:{k}") for k, ok in tabs if ok
    ]
    rows = [tab_btns[i : i + 2] for i in range(0, len(tab_btns), 2)]
    fav = is_fav(uid, cid)
    rows.append(
        [btn(t(lang, "apply"), f"apply:{cid}"), btn(t(lang, "fav_del" if fav else "fav_add"), f"fav:{cid}:{tab}")]
    )
    if c.get("gallery"):
        rows.append([btn(t(lang, "gallery"), f"gal:{cid}")])
    rows.append([btn(t(lang, "back"), f"cat:{c['category']}:0"), btn(t(lang, "home"), "home")])
    return fit(card_lines(c, lang, tab)), kb(rows), photo_of(c.get("image"))


def v_favs(lang: str, uid: int):
    ids = [r["cid"] for r in db.execute("SELECT cid FROM favs WHERE uid=? ORDER BY rowid DESC", (uid,))]
    items = [COURSES[i] for i in ids if i in COURSES]
    if not items:
        return t(lang, "favs_empty"), kb([[btn(t(lang, "m_courses"), "cats"), btn(t(lang, "home"), "home")]]), None
    rows = [course_button(c, lang) for c in items[:20]] + [[btn(t(lang, "home"), "home")]]
    return t(lang, "favs_title"), kb(rows), None


def v_faq(lang: str):
    rows = [[btn(clip(L(f["q"], lang), 60), f"faq:{i}")] for i, f in enumerate(SET.get("faq", []))]
    rows.append([btn(t(lang, "home"), "home")])
    return t(lang, "faq_title"), kb(rows), None


def v_faq_item(lang: str, i: int):
    f = SET["faq"][i]
    text = f"❓ <b>{esc(L(f['q'], lang))}</b>\n\n{esc(L(f['a'], lang))}"
    return text, kb([[btn(t(lang, "back"), "faq"), btn(t(lang, "apply"), "apply:0")]]), None


def v_about(lang: str):
    text = L(SET.get("about"), lang) + "\n\n" + t(lang, "about_back")
    return text, kb([[btn(t(lang, "m_apply"), "apply:0")], [btn(t(lang, "home"), "home")]]), None


def v_contact(lang: str):
    c = SET.get("contact", {})
    lines = [t(lang, "contact_title"), ""]
    if c.get("phone"):
        lines.append(f"📞 {t(lang, 'c_phone')}: <b>{esc(c['phone'])}</b>")
    if c.get("address"):
        lines.append(f"📍 {t(lang, 'c_address')}: {esc(L(c['address'], lang))}")
    if c.get("hours"):
        lines.append(f"🕘 {t(lang, 'c_hours')}: {esc(L(c['hours'], lang))}")
    rows = []
    links = [("c_tg", c.get("telegram")), ("c_ig", c.get("instagram")), ("c_web", c.get("website"))]
    links = [InlineKeyboardButton(text=t(lang, k), url=u) for k, u in links if u]
    rows += [links[i : i + 2] for i in range(0, len(links), 2)]
    rows.append([btn(t(lang, "m_apply"), "apply:0")])
    rows.append([btn(t(lang, "home"), "home")])
    return "\n".join(lines), kb(rows), None


def v_lang(lang: str):
    rows = [[btn(label, f"setlang:{code}")] for code, label in LANGS.items()]
    rows.append([btn(t(lang, "back"), "home")])
    return t(lang, "lang_title"), kb(rows), None


def search_courses(q: str, lang: str) -> list[dict]:
    q = q.lower().strip()
    words = [w for w in re.split(r"\s+", q) if w]
    found = []
    for c in COURSES.values():
        blob = " ".join(
            [
                *(L(c.get(k), lg) for k in ("title", "short", "description") for lg in ("uz", "ru")),
                *(L(CATS[c["category"]]["title"], lg) for lg in ("uz", "ru")),
            ]
        ).lower()
        if all(w in blob for w in words):
            found.append(c)
    return found


def v_search(lang: str, q: str):
    items = search_courses(q, lang)
    if not items:
        return t(lang, "search_none", q=esc(q)), kb([[btn(t(lang, "home"), "home")]]), None
    rows = [course_button(c, lang) for c in items[:8]] + [[btn(t(lang, "home"), "home")]]
    return t(lang, "search_res", q=esc(q)), kb(rows), None


# ----------------------------------------------------------------- ko'rsatish yordamchilari
async def send_view(m: Message, view) -> None:
    text, markup, photo = view
    if photo is not None:
        try:
            await m.answer_photo(photo, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest:
            log.warning("Rasm yuborilmadi, matn bilan almashtirildi")
    await m.answer(text, reply_markup=markup)


async def render(cb: CallbackQuery, view) -> None:
    """Joriy xabarni yangi ko'rinish bilan almashtiradi (matn <-> rasm o'tishlari bilan)."""
    text, markup, photo = view
    msg = cb.message
    if photo is None and not msg.photo:
        try:
            await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise
        return
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    await send_view(msg, view)


async def edit_card(cb: CallbackQuery, view) -> None:
    text, markup, _ = view
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=text, reply_markup=markup)
        else:
            await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# ----------------------------------------------------------------- buyruqlar
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    u = get_user(m.from_user)
    db.execute("UPDATE users SET name=?, username=? WHERE id=?", (m.from_user.full_name, m.from_user.username, m.from_user.id))
    db.commit()
    if not u["lang"]:
        rows = [[btn(label, f"setlang:{code}")] for code, label in LANGS.items()]
        await m.answer(t("uz", "choose_lang"), reply_markup=kb(rows))
        return
    await send_view(m, v_home(u["lang"], m.from_user.full_name))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    lang = lang_of(m.from_user)
    await m.answer(t(lang, "cancelled"), reply_markup=ReplyKeyboardRemove())
    await send_view(m, v_home(lang))


@router.message(Command("courses"))
async def cmd_courses(m: Message):
    await send_view(m, v_cats(lang_of(m.from_user)))


@router.message(Command("favorites"))
async def cmd_favs(m: Message):
    await send_view(m, v_favs(lang_of(m.from_user), m.from_user.id))


@router.message(Command("contact"))
async def cmd_contact(m: Message):
    await send_view(m, v_contact(lang_of(m.from_user)))


@router.message(Command("lang"))
async def cmd_lang(m: Message):
    await send_view(m, v_lang(lang_of(m.from_user)))


@router.message(Command("help"))
async def cmd_help(m: Message):
    lang = lang_of(m.from_user)
    await m.answer(t(lang, "help"), reply_markup=v_home(lang)[1])


# ----------------------------------------------------------------- navigatsiya (callback)
@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(cb: CallbackQuery, state: FSMContext):
    code = cb.data.split(":")[1]
    if code in LANGS:
        existing = get_user(cb.from_user)
        first_time = not existing["lang"]
        db.execute("UPDATE users SET lang=? WHERE id=?", (code, cb.from_user.id))
        db.commit()
        await state.clear()
        await render(cb, v_home(code, cb.from_user.full_name if first_time else None))
        if not first_time:
            await cb.answer(t(code, "lang_saved"))
            return
    await cb.answer()


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    await render(cb, v_home(lang_of(cb.from_user)))
    await cb.answer()


@router.callback_query(F.data == "cats")
async def cb_cats(cb: CallbackQuery):
    await render(cb, v_cats(lang_of(cb.from_user)))
    await cb.answer()


@router.callback_query(F.data.startswith("cat:"))
async def cb_cat(cb: CallbackQuery):
    _, cat_id, page = cb.data.split(":")
    await render(cb, v_list(lang_of(cb.from_user), cat_id, int(page)))
    await cb.answer()


@router.callback_query(F.data.startswith("c:"))
async def cb_course(cb: CallbackQuery):
    _, cid, tab = cb.data.split(":")
    if cid not in COURSES:
        await cb.answer()
        return
    view = v_card(lang_of(cb.from_user), cb.from_user.id, cid, tab)
    if cb.message.photo and view[2] is not None:
        await edit_card(cb, view)  # shu kartochka ichida bo'limni almashtirish
    else:
        await render(cb, view)
    await cb.answer()


@router.callback_query(F.data.startswith("fav:"))
async def cb_fav(cb: CallbackQuery):
    _, cid, tab = cb.data.split(":")
    if cid not in COURSES:
        await cb.answer()
        return
    lang = lang_of(cb.from_user)
    if is_fav(cb.from_user.id, cid):
        db.execute("DELETE FROM favs WHERE uid=? AND cid=?", (cb.from_user.id, cid))
        note = t(lang, "fav_removed")
    else:
        db.execute("INSERT OR IGNORE INTO favs(uid, cid) VALUES(?,?)", (cb.from_user.id, cid))
        note = t(lang, "fav_added")
    db.commit()
    await edit_card(cb, v_card(lang, cb.from_user.id, cid, tab))
    await cb.answer(note)


@router.callback_query(F.data.startswith("gal:"))
async def cb_gallery(cb: CallbackQuery):
    cid = cb.data.split(":")[1]
    c = COURSES.get(cid)
    if not c:
        await cb.answer()
        return
    media = [InputMediaPhoto(media=photo_of(x)) for x in (c.get("gallery") or [])[:10] if photo_of(x)]
    await cb.answer()
    if len(media) == 1:
        await cb.message.answer_photo(media[0].media)
    elif media:
        await cb.message.answer_media_group(media)


@router.callback_query(F.data == "favs")
async def cb_favs(cb: CallbackQuery):
    await render(cb, v_favs(lang_of(cb.from_user), cb.from_user.id))
    await cb.answer()


@router.callback_query(F.data == "faq")
async def cb_faq(cb: CallbackQuery):
    await render(cb, v_faq(lang_of(cb.from_user)))
    await cb.answer()


@router.callback_query(F.data.startswith("faq:"))
async def cb_faq_item(cb: CallbackQuery):
    i = int(cb.data.split(":")[1])
    if 0 <= i < len(SET.get("faq", [])):
        await render(cb, v_faq_item(lang_of(cb.from_user), i))
    await cb.answer()


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    await render(cb, v_about(lang_of(cb.from_user)))
    await cb.answer()


@router.callback_query(F.data == "contact")
async def cb_contact(cb: CallbackQuery):
    await render(cb, v_contact(lang_of(cb.from_user)))
    await cb.answer()


@router.callback_query(F.data == "lang")
async def cb_lang(cb: CallbackQuery):
    await render(cb, v_lang(lang_of(cb.from_user)))
    await cb.answer()


@router.callback_query(F.data == "search")
async def cb_search(cb: CallbackQuery):
    lang = lang_of(cb.from_user)
    await render(cb, (t(lang, "search_prompt"), kb([[btn(t(lang, "home"), "home")]]), None))
    await cb.answer()


# ----------------------------------------------------------------- ariza (lead) formasi
class Lead(StatesGroup):
    name = State()
    phone = State()
    comment = State()


CANCEL_TEXTS = {t(lg, "cancel") for lg in LANGS}
SKIP_TEXTS = {t(lg, "skip") for lg in LANGS}
PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{7,17}$")


def reply_kb(lang: str, phone: bool = False, skip: bool = False) -> ReplyKeyboardMarkup:
    rows = []
    if phone:
        rows.append([KeyboardButton(text=t(lang, "share_phone"), request_contact=True)])
    if skip:
        rows.append([KeyboardButton(text=t(lang, "skip"))])
    rows.append([KeyboardButton(text=t(lang, "cancel"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


@router.callback_query(F.data.startswith("apply:"))
async def cb_apply(cb: CallbackQuery, state: FSMContext):
    cid = cb.data.split(":")[1]
    lang = lang_of(cb.from_user)
    course_title = L(COURSES[cid]["title"], lang) if cid in COURSES else None
    label = t(lang, "course_label", title=esc(course_title)) if course_title else t(lang, "general_course")
    await state.clear()
    await state.update_data(cid=cid if cid in COURSES else "", course=L(COURSES[cid]["title"], "uz") if cid in COURSES else t("uz", "general_course"))
    await state.set_state(Lead.name)
    await cb.message.answer(t(lang, "ask_name", course=label), reply_markup=reply_kb(lang))
    await cb.answer()


@router.message(StateFilter(Lead.name, Lead.phone, Lead.comment), F.text.in_(CANCEL_TEXTS))
async def lead_cancel(m: Message, state: FSMContext):
    await state.clear()
    lang = lang_of(m.from_user)
    await m.answer(t(lang, "cancelled"), reply_markup=ReplyKeyboardRemove())
    await send_view(m, v_home(lang))


@router.message(Lead.name, F.text, ~F.text.startswith("/"))
async def lead_name(m: Message, state: FSMContext):
    lang = lang_of(m.from_user)
    await state.update_data(name=clip(m.text, 60))
    await state.set_state(Lead.phone)
    await m.answer(t(lang, "ask_phone"), reply_markup=reply_kb(lang, phone=True))


@router.message(Lead.phone, F.contact)
async def lead_phone_contact(m: Message, state: FSMContext):
    await _ask_comment(m, state, m.contact.phone_number)


@router.message(Lead.phone, F.text, ~F.text.startswith("/"))
async def lead_phone_text(m: Message, state: FSMContext):
    lang = lang_of(m.from_user)
    phone = m.text.strip()
    if not PHONE_RE.match(phone):
        await m.answer(t(lang, "bad_phone"), reply_markup=reply_kb(lang, phone=True))
        return
    await _ask_comment(m, state, phone)


async def _ask_comment(m: Message, state: FSMContext, phone: str):
    lang = lang_of(m.from_user)
    await state.update_data(phone=phone)
    await state.set_state(Lead.comment)
    await m.answer(t(lang, "ask_comment"), reply_markup=reply_kb(lang, skip=True))


@router.message(Lead.comment, F.text, ~F.text.startswith("/"))
async def lead_comment(m: Message, state: FSMContext):
    comment = "" if m.text in SKIP_TEXTS else clip(m.text, 500)
    await finish_lead(m, state, comment)


async def finish_lead(m: Message, state: FSMContext, comment: str):
    lang = lang_of(m.from_user)
    d = await state.get_data()
    await state.clear()
    u = m.from_user
    db.execute(
        "INSERT INTO leads(uid, username, course, name, phone, comment, ts) VALUES(?,?,?,?,?,?,?)",
        (u.id, u.username, d.get("course"), d.get("name"), d.get("phone"), comment, int(time.time())),
    )
    db.commit()
    who = f'<a href="tg://user?id={u.id}">{esc(u.full_name)}</a>' + (f" (@{esc(u.username)})" if u.username else "")
    text = (
        "🆕 <b>Yangi ariza</b>\n\n"
        f"📚 Kurs: <b>{esc(d.get('course'))}</b>\n"
        f"👤 Ism: {esc(d.get('name'))}\n"
        f"📞 Telefon: <code>{esc(d.get('phone'))}</code>\n"
        f"💬 Izoh: {esc(comment) or '—'}\n"
        f"🔗 Telegram: {who}\n"
        f"🌐 Til: {lang}"
    )
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
        except Exception:
            log.exception("Adminga yuborilmadi: %s", admin)
    await m.answer(t(lang, "lead_ok"), reply_markup=ReplyKeyboardRemove())
    await send_view(m, v_home(lang))


# ----------------------------------------------------------------- admin
class Broadcast(StatesGroup):
    msg = State()


def admin_kb() -> InlineKeyboardMarkup:
    return kb(
        [
            [btn("📊 Statistika", "adm:stats"), btn("📥 Arizalar", "adm:leads")],
            [btn("📄 Arizalar (CSV)", "adm:csv"), btn("📣 Xabar yuborish", "adm:bc")],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_kb())


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Sizning ID: <code>{m.from_user.id}</code>")


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer()
        return
    action = cb.data.split(":")[1]
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
    if action == "stats":
        now = int(time.time())
        top = db.execute(
            "SELECT cid, COUNT(*) n FROM favs GROUP BY cid ORDER BY n DESC LIMIT 5"
        ).fetchall()
        top_txt = "\n".join(f"• {esc(L(COURSES[r['cid']]['title'], 'uz'))}: {r['n']}" for r in top if r["cid"] in COURSES) or "—"
        text = (
            "📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{q('SELECT COUNT(*) FROM users')}</b>\n"
            f"   🇺🇿 {q('SELECT COUNT(*) FROM users WHERE lang=?', 'uz')} · 🇷🇺 {q('SELECT COUNT(*) FROM users WHERE lang=?', 'ru')}\n"
            f"🆕 Oxirgi 7 kun: {q('SELECT COUNT(*) FROM users WHERE joined>?', now - 7 * 86400)}\n"
            f"📝 Arizalar: <b>{q('SELECT COUNT(*) FROM leads')}</b> (7 kun: {q('SELECT COUNT(*) FROM leads WHERE ts>?', now - 7 * 86400)})\n\n"
            f"❤️ Eng ko‘p tanlangan kurslar:\n{top_txt}"
        )
        await cb.message.answer(text)
    elif action == "leads":
        rows = db.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            await cb.message.answer("Hali arizalar yo‘q.")
        for r in rows:
            when = time.strftime("%d.%m %H:%M", time.gmtime(r["ts"] + 5 * 3600))
            await cb.message.answer(
                f"#{r['id']} · {when}\n📚 {esc(r['course'])}\n👤 {esc(r['name'])} · <code>{esc(r['phone'])}</code>\n"
                f"💬 {esc(r['comment']) or '—'}\n🔗 <a href=\"tg://user?id={r['uid']}\">profil</a>"
            )
    elif action == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "date", "course", "name", "phone", "comment", "telegram_id", "username"])
        for r in db.execute("SELECT * FROM leads ORDER BY id"):
            w.writerow([r["id"], time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"])), r["course"], r["name"], r["phone"], r["comment"], r["uid"], r["username"]])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")
        await cb.message.answer_document(BufferedInputFile(data, filename="leads.csv"))
    elif action == "bc":
        await state.set_state(Broadcast.msg)
        await cb.message.answer("📣 Yuboriladigan xabarni jo‘nating (matn, rasm, video — istalgan). Bekor qilish: /cancel")
    await cb.answer()


@router.message(Broadcast.msg)
async def bc_preview(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or (m.text and m.text.startswith("/")):
        return
    total = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    await state.update_data(mid=m.message_id, chat=m.chat.id)
    await m.answer(
        f"Yuqoridagi xabar <b>{total}</b> ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
        reply_markup=kb([[btn("✅ Yuborish", "bc:go"), btn("❌ Bekor", "bc:no")]]),
    )


_tasks: set = set()


@router.callback_query(F.data.startswith("bc:"))
async def bc_confirm(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer()
        return
    d = await state.get_data()
    await state.clear()
    if cb.data == "bc:no" or "mid" not in d:
        await cb.message.edit_text("Bekor qilindi.")
        await cb.answer()
        return
    await cb.message.edit_text("⏳ Yuborish boshlandi...")
    task = asyncio.create_task(run_broadcast(cb.from_user.id, d["chat"], d["mid"]))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    await cb.answer()


async def run_broadcast(admin_id: int, from_chat: int, mid: int) -> None:
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE active=1")]
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, from_chat, mid)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(uid, from_chat, mid)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            db.execute("UPDATE users SET active=0 WHERE id=?", (uid,))
            db.commit()
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ~20 xabar/soniya — Telegram limitidan past
    await bot.send_message(admin_id, f"✅ Yuborish tugadi.\nYetkazildi: <b>{ok}</b>\nXato / bloklagan: <b>{fail}</b>")


# ----------------------------------------------------------------- qidiruv (oddiy matn)
@router.message(StateFilter(None), F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def on_text(m: Message):
    u = get_user(m.from_user)
    if not u["lang"]:
        rows = [[btn(label, f"setlang:{code}")] for code, label in LANGS.items()]
        await m.answer(t("uz", "choose_lang"), reply_markup=kb(rows))
        return
    await send_view(m, v_search(u["lang"], m.text))


@router.message(StateFilter(None), F.chat.type == "private")
async def on_other(m: Message):
    await m.answer(t(lang_of(m.from_user), "help"), reply_markup=v_home(lang_of(m.from_user))[1])


# ----------------------------------------------------------------- ishga tushirish
COMMANDS = {
    "uz": [("start", "Botni ishga tushirish"), ("courses", "Kurslar"), ("favorites", "Tanlanganlar"),
           ("contact", "Aloqa"), ("lang", "Tilni o‘zgartirish")],
    "ru": [("start", "Запустить бота"), ("courses", "Курсы"), ("favorites", "Избранное"),
           ("contact", "Контакты"), ("lang", "Сменить язык")],
}


async def health(request: web.Request):
    return web.Response(text="ok")


async def on_startup(app: web.Application):
    for code, cmds in COMMANDS.items():
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in cmds], language_code=code)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS["uz"]])
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS bo'sh — arizalar hech kimga yuborilmaydi! Bot ichida /myid yuborib ID'ingizni oling.")
    if BASE_URL:
        await bot.set_webhook(
            f"{BASE_URL}/webhook", secret_token=WEBHOOK_SECRET, allowed_updates=dp.resolve_used_update_types()
        )
        log.info("Webhook o'rnatildi: %s/webhook", BASE_URL)
    else:
        await bot.delete_webhook()
        app["poll"] = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        log.info("Lokal polling rejimi")


async def on_cleanup(app: web.Application):
    task = app.get("poll")
    if task:
        task.cancel()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT)
