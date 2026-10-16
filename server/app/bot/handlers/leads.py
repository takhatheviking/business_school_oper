"""A lead from a client: course (or a general consultation) -> name -> phone -> question -> send.
The lead goes to the leads chat, where sales take it."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import LeadCb
from app.bot.keyboards import main_menu
from app.bot.states import LeadSG
from app.config import Settings
from app.db.models import Course, CourseStatus, User
from app.formatting import esc, normalize_phone
from app.i18n import Translator, all_variants
from app.services import courses as courses_svc
from app.services.lead_cards import post_lead

router = Router(name="leads")
router.message.filter(F.chat.type == "private")

NAME_MAX, COMMENT_MAX = 100, 1000


def reply_kb(*rows: list[KeyboardButton]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=list(rows), resize_keyboard=True, one_time_keyboard=True)


def interest_title(course: Course | None, tr: Translator) -> str:
    return course.loc("title", tr.lang) if course else tr("lead_consultation")


async def choose_course(message: Message, tr: Translator, session: AsyncSession) -> None:
    b = InlineKeyboardBuilder()
    for c in await courses_svc.list_published(session):
        b.button(text=c.loc("title", tr.lang)[:60], callback_data=LeadCb(action="start", course_id=c.id))
    b.button(text=tr("btn_consult"), callback_data=LeadCb(action="start"))
    b.adjust(1)
    await message.answer(tr("lead_choose"), reply_markup=b.as_markup())


@router.message(F.text.in_(all_variants("btn_lead")))
async def menu_lead(message: Message, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await choose_course(message, tr, session)


@router.callback_query(LeadCb.filter(F.action == "start"))
async def cb_start(cb: CallbackQuery, callback_data: LeadCb, user: User, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    course_id = callback_data.course_id or None
    if course_id is not None:
        course = await session.get(Course, course_id)
        if course is None or course.status != CourseStatus.PUBLISHED:
            await cb.answer(tr("err_course_unavailable"), show_alert=True)
            return
    existing = await courses_svc.open_lead(session, user.id, course_id)
    await cb.answer()
    if existing is not None:
        await cb.message.answer(tr("lead_exists", id=existing.id))
        return
    await state.clear()
    await state.set_state(LeadSG.name)
    await state.update_data(course_id=course_id)
    suggested = await courses_svc.last_lead_name(session, user.id) or user.full_name
    await cb.message.answer(
        tr("lead_ask_name"),
        reply_markup=reply_kb([KeyboardButton(text=suggested[:NAME_MAX])], [KeyboardButton(text=tr("btn_cancel"))]),
    )


async def ask_comment(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.set_state(LeadSG.comment)
    await message.answer(tr("lead_ask_comment"), reply_markup=reply_kb([KeyboardButton(text=tr("btn_skip"))], [KeyboardButton(text=tr("btn_cancel"))]))


@router.message(LeadSG.name, F.text)
async def on_name(message: Message, user: User, tr: Translator, state: FSMContext) -> None:
    name = " ".join(message.text.split())
    if not 2 <= len(name) <= NAME_MAX:
        await message.answer(tr("lead_err_name"))
        return
    await state.update_data(name=name)
    if user.phone:
        await ask_comment(message, tr, state)
        return
    await state.set_state(LeadSG.phone)
    await message.answer(
        tr("lead_ask_phone"),
        reply_markup=reply_kb([KeyboardButton(text=tr("btn_share_contact"), request_contact=True)], [KeyboardButton(text=tr("btn_cancel"))]),
    )


@router.message(LeadSG.phone, F.contact)
async def on_phone_contact(message: Message, user: User, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    if message.contact.user_id != message.from_user.id:
        await message.answer(tr("err_foreign_contact"))
        return
    phone = message.contact.phone_number
    user.phone = phone if phone.startswith("+") else f"+{phone}"
    await session.commit()
    await ask_comment(message, tr, state)


@router.message(LeadSG.phone, F.text)
async def on_phone_text(message: Message, user: User, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    phone = normalize_phone(message.text)
    if phone is None:
        await message.answer(tr("lead_err_phone"))
        return
    user.phone = phone
    await session.commit()
    await ask_comment(message, tr, state)


@router.message(LeadSG.comment, F.text)
async def on_comment(message: Message, user: User, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    skipped = message.text in all_variants("btn_skip")
    if not skipped and len(message.text) > COMMENT_MAX:
        await message.answer(tr("lead_err_comment", max=COMMENT_MAX))
        return
    await state.update_data(comment=None if skipped else message.text.strip())
    await state.set_state(LeadSG.confirm)
    data = await state.get_data()
    course = await session.get(Course, data["course_id"]) if data.get("course_id") else None
    await message.answer(tr("lead_almost"), reply_markup=main_menu(tr, user))
    b = InlineKeyboardBuilder()
    b.button(text=tr("lead_btn_send"), callback_data=LeadCb(action="send", course_id=data.get("course_id") or 0))
    b.button(text=tr("btn_cancel"), callback_data=LeadCb(action="abort"))
    b.adjust(1)
    summary = tr(
        "lead_summary",
        interest=esc(interest_title(course, tr)),
        name=esc(data["name"]),
        phone=esc(user.phone or "—"),
        comment=esc(data.get("comment") or "—"),
    )
    await message.answer(summary, reply_markup=b.as_markup())


@router.message(LeadSG.name)
@router.message(LeadSG.phone)
@router.message(LeadSG.comment)
async def on_not_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("lead_send_text"))


@router.callback_query(LeadCb.filter(F.action == "send"), LeadSG.confirm)
async def cb_send(cb: CallbackQuery, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    lead, created = await courses_svc.create_lead(
        session,
        user=user,
        course_id=data.get("course_id"),
        name=data["name"],
        phone=user.phone or "",
        comment=data.get("comment"),
        lang=tr.lang,
    )
    if not created:
        await cb.message.answer(tr("lead_exists", id=lead.id))
        return
    await post_lead(bot, session, settings, lead)
    await cb.message.answer(tr("lead_sent", id=lead.id), reply_markup=main_menu(tr, user))


@router.callback_query(LeadCb.filter(F.action == "abort"))
async def cb_abort(cb: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await cb.message.edit_text(tr("lead_aborted"))
