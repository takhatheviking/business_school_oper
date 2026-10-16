"""Admin: course catalog. A course is created from one filled form (texts in RU / UZ / EN,
start date, price), then the poster; it is edited the same way."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.create import FLAGS, form_errors_text, pre
from app.bot.keyboards import admin_back
from app.bot.states import CourseCreateSG, CourseEditSG
from app.db.models import Course, CourseStatus, LeadStatus, User
from app.formatting import esc, fmt_date, fmt_money
from app.i18n import Translator
from app.services import courses as courses_svc
from app.services.course_form import CourseForm, blank_form, example_form, filled_form, label_of, parse_form
from app.services.event_form import SKIP

router = Router(name="admin_courses")


def price_text(price_tiyin: int | None, tr: Translator) -> str:
    return fmt_money(price_tiyin, tr.lang) if price_tiyin else tr("course_price_on_request")


def cancel_kb(tr: Translator, action: str = "courses", course_id: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action=action, item_id=course_id))
    return b.as_markup()


# --- List --------------------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "courses"))
async def cb_courses(cb: CallbackQuery, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    courses = await courses_svc.list_for_admin(session)
    b = InlineKeyboardBuilder()
    for c in courses:
        mark = "🟢" if c.status == CourseStatus.PUBLISHED else "📝"
        when = f"{c.starts_on:%d.%m.%y} · " if c.starts_on else ""
        b.button(text=f"{mark} {when}{c.loc('title', tr.lang)}"[:64], callback_data=AdminCb(action="course", item_id=c.id))
    b.adjust(1)
    b.row(InlineKeyboardButton(text=tr("adm_btn_new_course"), callback_data=AdminCb(action="new_course").pack()))
    b.row(admin_back(tr))
    await cb.answer()
    text = tr("adm_courses_title", n=len(courses)) if courses else tr("adm_courses_empty")
    if cb.message.photo:
        await cb.message.answer(text, reply_markup=b.as_markup())
    else:
        await cb.message.edit_text(text, reply_markup=b.as_markup())


# --- Card --------------------------------------------------------------------------------

async def course_card(session: AsyncSession, course: Course, tr: Translator, bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    counts = await courses_svc.lead_counts(session, course.id)
    me = await bot.me()
    lines = [
        f"<b>{esc(course.loc('title', tr.lang))}</b> · {tr(f'adm_status_{course.status}')}",
        tr("course_start", date=fmt_date(course.starts_on, tr.lang)) if course.starts_on else tr("adm_course_no_start"),
    ]
    schedule = course.loc("schedule", tr.lang)
    if schedule:
        lines.append(f"🕘 {esc(schedule)}")
    lines += [
        f"💵 {price_text(course.price_tiyin, tr)}",
        "",
        tr(
            "adm_course_leads",
            new=counts[LeadStatus.NEW],
            in_work=counts[LeadStatus.IN_WORK],
            enrolled=counts[LeadStatus.ENROLLED],
            rejected=counts[LeadStatus.REJECTED],
        ),
        "",
        tr("adm_share_link", link=f"https://t.me/{me.username}?start=c_{course.id}"),
    ]
    b = InlineKeyboardBuilder()
    cid = course.id
    if course.status == CourseStatus.DRAFT:
        b.button(text=tr("adm_btn_publish"), callback_data=AdminCb(action="course_pub", item_id=cid, value="1"))
    else:
        b.button(text=tr("adm_btn_unpublish"), callback_data=AdminCb(action="course_pub", item_id=cid, value="0"))
    b.button(text=tr("adm_btn_course_edit"), callback_data=AdminCb(action="course_edit", item_id=cid))
    b.button(text=tr("field_poster"), callback_data=AdminCb(action="course_poster", item_id=cid))
    b.button(text=tr("btn_back"), callback_data=AdminCb(action="courses"))
    b.adjust(1, 2, 1)
    return "\n".join(lines), b.as_markup()


async def show_course_card(target: Message, session: AsyncSession, course_id: int, tr: Translator, bot: Bot, edit: bool) -> None:
    course = await session.get(Course, course_id, populate_existing=True)
    if course is None:
        await target.answer(tr("err_course_unavailable"))
        return
    text, kb = await course_card(session, course, tr, bot)
    if edit and not (target.photo or target.document):
        await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(AdminCb.filter(F.action == "course"))
async def cb_course(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await show_course_card(cb.message, session, callback_data.item_id, tr, bot, edit=True)


@router.callback_query(AdminCb.filter(F.action == "course_pub"))
async def cb_publish(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, bot: Bot) -> None:
    course = await session.get(Course, callback_data.item_id)
    if course is None:
        await cb.answer(tr("err_course_unavailable"), show_alert=True)
        return
    course.status = CourseStatus.PUBLISHED if callback_data.value == "1" else CourseStatus.DRAFT
    await session.commit()
    await cb.answer(tr("saved"))
    await show_course_card(cb.message, session, course.id, tr, bot, edit=True)


# --- Create ------------------------------------------------------------------------------

async def send_form(message: Message, tr: Translator, form_text: str, intro_key: str, cancel: AdminCb) -> None:
    b = InlineKeyboardBuilder()
    b.button(text=tr("form_btn_example"), callback_data=AdminCb(action="course_example"))
    b.button(text=tr("btn_cancel"), callback_data=cancel)
    b.adjust(1)
    await message.answer(tr(intro_key), reply_markup=b.as_markup())
    await message.answer(pre(form_text))


def preview_text(form: CourseForm, tr: Translator) -> str:
    parts = [tr("course_preview_header")]
    for lang, flag in FLAGS.items():
        title = form.texts["title"].get(lang) or form.texts["title"]["ru"]
        schedule = form.texts["schedule"].get(lang) or form.texts["schedule"].get("ru")
        description = form.texts["description"].get(lang) or form.texts["description"].get("ru") or ""
        if len(description) > 300:
            description = description[:300] + "…"
        block = [f"{flag} <b>{esc(title)}</b>"]
        if schedule:
            block.append(f"🕘 {esc(schedule)}")
        if description:
            block.append(esc(description))
        parts.append("\n".join(block))
    start = tr("course_start", date=fmt_date(form.starts_on, tr.lang)) if form.starts_on else tr("adm_course_no_start")
    parts.append(f"{start}\n💵 {price_text(form.price_tiyin, tr)}")
    if form.missing:
        parts.append(tr("form_missing", fields=", ".join(esc(label_of(k, tr.lang)) for k in form.missing)))
    return "\n\n".join(parts)


@router.callback_query(AdminCb.filter(F.action == "new_course"))
async def cb_new_course(cb: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CourseCreateSG.form)
    await cb.answer()
    await send_form(cb.message, tr, blank_form(tr.lang), "course_form_intro", AdminCb(action="courses"))


@router.callback_query(AdminCb.filter(F.action == "course_example"))
async def cb_example(cb: CallbackQuery, tr: Translator) -> None:
    await cb.answer()
    await cb.message.answer(tr("form_example_intro") + "\n\n" + pre(example_form(tr.lang)))


@router.message(CourseCreateSG.form, F.text)
async def on_form(message: Message, tr: Translator, state: FSMContext) -> None:
    form, errors = parse_form(message.text, ui_lang=tr.lang)
    if errors:
        await message.answer(form_errors_text(tr, errors, message.text))
        return
    await state.update_data(form=form.to_state(), form_text=message.text)
    await state.set_state(CourseCreateSG.poster)
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_skip"), callback_data=AdminCb(action="course_no_poster"))
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action="courses"))
    b.adjust(1)
    await message.answer(tr("course_ask_poster"), reply_markup=b.as_markup())


@router.message(CourseCreateSG.form)
async def on_form_not_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("form_send_text"))


async def show_preview(message: Message, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    form = CourseForm.from_state(data["form"])
    await state.set_state(CourseCreateSG.confirm)
    b = InlineKeyboardBuilder()
    b.button(text=tr("adm_btn_publish"), callback_data=AdminCb(action="course_save", value="publish"))
    b.button(text=tr("wiz_btn_draft"), callback_data=AdminCb(action="course_save", value="draft"))
    b.button(text=tr("form_btn_fix"), callback_data=AdminCb(action="course_fix"))
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action="courses"))
    b.adjust(1)
    if data.get("poster"):
        await message.answer_photo(data["poster"])
    await message.answer(preview_text(form, tr), reply_markup=b.as_markup(), disable_web_page_preview=True)


@router.message(CourseCreateSG.poster, F.photo)
@router.message(CourseCreateSG.poster, F.text.in_(SKIP))
async def on_poster(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.update_data(poster=message.photo[-1].file_id if message.photo else None)
    await show_preview(message, tr, state)


@router.callback_query(AdminCb.filter(F.action == "course_no_poster"), CourseCreateSG.poster)
async def cb_no_poster(cb: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await cb.answer()
    await state.update_data(poster=None)
    await show_preview(cb.message, tr, state)


@router.message(CourseCreateSG.poster)
async def on_poster_other(message: Message, tr: Translator) -> None:
    await message.answer(tr("edit_err_photo"))


@router.callback_query(AdminCb.filter(F.action == "course_fix"), CourseCreateSG.confirm)
async def cb_fix(cb: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(CourseCreateSG.form)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await send_form(cb.message, tr, data.get("form_text") or blank_form(tr.lang), "form_fix_intro", AdminCb(action="courses"))


@router.callback_query(AdminCb.filter(F.action == "course_save"), CourseCreateSG.confirm)
async def cb_save(cb: CallbackQuery, callback_data: AdminCb, user: User, tr: Translator, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    form = CourseForm.from_state(data["form"])
    course = Course(
        title=form.texts["title"]["ru"],
        poster_file_id=data.get("poster"),
        status=CourseStatus.PUBLISHED if callback_data.value == "publish" else CourseStatus.DRAFT,
        created_by_id=user.id,
    )
    form.apply_to(course)
    session.add(course)
    await session.commit()
    await cb.answer(tr("saved"))
    await cb.message.edit_reply_markup(reply_markup=None)
    await show_course_card(cb.message, session, course.id, tr, bot, edit=False)


# --- Edit --------------------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "course_edit"))
async def cb_edit(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    course = await session.get(Course, callback_data.item_id)
    if course is None:
        await cb.answer(tr("err_course_unavailable"), show_alert=True)
        return
    await state.clear()
    await state.set_state(CourseEditSG.form)
    await state.update_data(course_id=course.id)
    await cb.answer()
    await cb.message.answer(tr("course_edit_intro"), reply_markup=cancel_kb(tr, "course", course.id))
    await cb.message.answer(pre(filled_form(course, tr.lang)))


@router.message(CourseEditSG.form, F.text)
async def on_edit_form(message: Message, tr: Translator, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    course = await session.get(Course, (await state.get_data())["course_id"])
    if course is None:
        await state.clear()
        return
    form, errors = parse_form(message.text, ui_lang=tr.lang)
    if errors:
        await message.answer(form_errors_text(tr, errors, message.text))
        return
    form.apply_to(course)
    await session.commit()
    await state.clear()
    await message.answer(tr("saved"))
    await show_course_card(message, session, course.id, tr, bot, edit=False)


@router.callback_query(AdminCb.filter(F.action == "course_poster"))
async def cb_poster(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CourseEditSG.poster)
    await state.update_data(course_id=callback_data.item_id)
    await cb.answer()
    await cb.message.answer(tr("edit_prompt_poster"), reply_markup=cancel_kb(tr, "course", callback_data.item_id))


@router.message(CourseEditSG.poster, F.photo)
@router.message(CourseEditSG.poster, F.text.in_(SKIP))
async def on_new_poster(message: Message, tr: Translator, session: AsyncSession, state: FSMContext, bot: Bot) -> None:
    course = await session.get(Course, (await state.get_data())["course_id"])
    await state.clear()
    if course is None:
        return
    course.poster_file_id = message.photo[-1].file_id if message.photo else None
    await session.commit()
    await message.answer(tr("saved"))
    await show_course_card(message, session, course.id, tr, bot, edit=False)


# Registered after the handlers above: aiogram tries handlers in order.
@router.message(CourseEditSG.form)
async def on_edit_not_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("form_send_text"))


@router.message(CourseEditSG.poster)
async def on_edit_poster_other(message: Message, tr: Translator) -> None:
    await message.answer(tr("edit_err_photo"))
