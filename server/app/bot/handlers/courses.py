"""Course catalog for clients: the list, a course card with "Leave a request"."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import CourseCb, LeadCb
from app.db.models import Course, CourseStatus, Lead, User
from app.formatting import esc, fmt_date, fmt_money
from app.i18n import Translator, all_variants
from app.services import courses as courses_svc

router = Router(name="courses")
CAPTION_LIMIT = 1024


def course_text(course: Course, tr: Translator, lead: Lead | None = None) -> str:
    lines = [f"<b>{esc(course.loc('title', tr.lang))}</b>", ""]
    if course.starts_on:
        lines.append(tr("course_start", date=fmt_date(course.starts_on, tr.lang)))
    schedule = course.loc("schedule", tr.lang)
    if schedule:
        lines.append(f"🕘 {esc(schedule)}")
    price = fmt_money(course.price_tiyin, tr.lang) if course.price_tiyin else tr("course_price_on_request")
    lines.append(f"💵 {price}")
    description = course.loc("description", tr.lang)
    if description:
        lines += ["", esc(description)]
    if lead is not None:
        lines += ["", tr("course_lead_exists", id=lead.id)]
    return "\n".join(lines)


def course_kb(course: Course, tr: Translator, has_lead: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if not has_lead:
        b.button(text=tr("btn_enroll"), callback_data=LeadCb(action="start", course_id=course.id))
    b.button(text=tr("btn_all_courses"), callback_data=CourseCb(action="list"))
    b.adjust(1)
    return b.as_markup()


async def send_course_card(message: Message, course_id: int, *, user: User, tr: Translator, session: AsyncSession) -> None:
    course = await session.get(Course, course_id)
    if course is None or course.status != CourseStatus.PUBLISHED:
        await message.answer(tr("err_course_unavailable"))
        return
    lead = await courses_svc.open_lead(session, user.id, course.id)
    text, kb = course_text(course, tr, lead), course_kb(course, tr, lead is not None)
    if course.poster_file_id and len(text) <= CAPTION_LIMIT:
        await message.answer_photo(course.poster_file_id, caption=text, reply_markup=kb)
    elif course.poster_file_id:
        await message.answer_photo(course.poster_file_id)
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def courses_list_view(session: AsyncSession, tr: Translator) -> tuple[str, InlineKeyboardMarkup | None]:
    courses = await courses_svc.list_published(session)
    if not courses:
        return tr("no_courses"), None
    b = InlineKeyboardBuilder()
    for c in courses:
        when = f"{c.starts_on:%d.%m} · " if c.starts_on else ""
        b.button(text=f"{when}{c.loc('title', tr.lang)}"[:60], callback_data=CourseCb(action="open", course_id=c.id))
    b.button(text=tr("btn_consult"), callback_data=LeadCb(action="start"))
    b.adjust(1)
    return tr("courses_title", n=len(courses)), b.as_markup()


@router.message(F.chat.type == "private", F.text.in_(all_variants("btn_courses")))
async def show_courses(message: Message, tr: Translator, session: AsyncSession) -> None:
    text, kb = await courses_list_view(session, tr)
    await message.answer(text, reply_markup=kb)


@router.callback_query(CourseCb.filter(F.action == "list"))
async def cb_list(cb: CallbackQuery, tr: Translator, session: AsyncSession) -> None:
    text, kb = await courses_list_view(session, tr)
    await cb.answer()
    if cb.message.photo or cb.message.document:
        await cb.message.answer(text, reply_markup=kb)
    else:
        await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(CourseCb.filter(F.action == "open"))
async def cb_open(cb: CallbackQuery, callback_data: CourseCb, user: User, tr: Translator, session: AsyncSession) -> None:
    await cb.answer()
    await send_course_card(cb.message, callback_data.course_id, user=user, tr=tr, session=session)
