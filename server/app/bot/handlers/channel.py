"""«📣 Канал»: the content plan that the bot posts to the channel (app/services/autopost.py).

For admins and the content team (role "content"):
  - a banner or PDF sent to the bot is attached to a post of the plan: by the number in the
    caption («14»), or by a button among the nearest posts that need such a file;
  - the section shows the channel, the plan and the nearest posts; a post's card has a
    preview, "Publish now", "Do not publish" and "Remove the file";
  - admins give the plan's spreadsheet link; the channel is connected by adding the bot to it
    as an administrator (the offer comes to the super-admin, handlers/moderation.py).
"""

import logging
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import ChannelCb
from app.bot.middlewares import IsContent
from app.bot.states import ChannelSG
from app.config import Settings
from app.db.models import ChannelPost, User, UserRole
from app.formatting import esc, fmt_dt
from app.i18n import Translator, all_variants, t
from app.services import app_settings
from app.services.app_settings import ChannelInfo
from app.services.autopost import (
    GRACE,
    channel_name,
    detach,
    has_file,
    icon,
    is_out,
    mark_done_by_hand,
    planner,
    publish,
    send_post,
    short,
    states,
    what,
)
from app.services.autopost import attach as attach_file
from app.services.autopost import state as post_state
from app.services.booking import now_utc
from app.services.content_plan import DOCUMENT, MANUAL, PHOTO, Plan, PlanPost
from app.services.gsheets import describe_error, parse_sheet_id, service_account_email
from app.services.notify import alert_admins

log = logging.getLogger(__name__)
router = Router(name="channel")
router.message.filter(F.chat.type == "private", IsContent)
router.callback_query.filter(IsContent)
PAGE = 10
UPCOMING = 6
PICK = 8
NUMBER_RE = re.compile(r"\s*(?:№|#|no\.?|n|пост|post)?\s*(\d{1,4})\s*", re.IGNORECASE)


def is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


def number_in(caption: str | None) -> str | None:
    m = NUMBER_RE.fullmatch(caption or "")
    return str(int(m.group(1))) if m else None


def file_of(message: Message) -> dict | None:
    """{"file_id", "kind": photo | image | document, "name"} of a photo or a document."""
    if message.photo:
        return {"file_id": message.photo[-1].file_id, "kind": "photo", "name": None}
    doc = message.document
    if doc is None:
        return None
    mime, name = (doc.mime_type or "").lower(), doc.file_name or ""
    kind = "image" if mime.startswith("image/") and not name.lower().endswith(".pdf") else "document"
    return {"file_id": doc.file_id, "kind": kind, "name": name or None}


def fits(post: PlanPost, kind: str) -> bool:
    return (post.kind == PHOTO and kind in ("photo", "image")) or (post.kind == DOCUMENT and kind == "document")


def nav_back(tr: Translator, action: str = "home", page: int = 0) -> list:
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_back"), callback_data=ChannelCb(action=action, page=page))
    return b.export()[0]


async def edit_or_send(target: Message, text: str, kb: InlineKeyboardMarkup | None, edit: bool) -> None:
    if edit and target.text:
        try:
            await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
        except TelegramBadRequest:
            pass  # not modified, or too old to edit
    await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


# --- home ---------------------------------------------------------------------------------

async def home_view(session: AsyncSession, settings: Settings, tr: Translator, user: User, *, force: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    if force:
        try:
            await planner.refresh(session, settings, force=True)
        except Exception:  # noqa: BLE001 - shown below
            pass
    plan = await planner.current(session, settings)
    channel = await app_settings.get_content_channel(session, settings)
    lines = [
        tr("ch_title"),
        tr("ch_channel_line", channel=esc(channel_name(channel)) if channel else tr("ch_channel_none")),
    ]
    if plan is not None:
        lines.append(tr("ch_plan_line", url=esc(f"{plan.url}#gid={plan.gid}"), tab=esc(plan.tab), n=len(plan.posts)))
        if planner.read_at:
            lines.append(tr("ch_read_at", at=fmt_dt(planner.read_at, settings.tz, tr.lang, with_weekday=False)))
    else:
        lines.append(tr("ch_plan_none"))
    if planner.last_error:
        lines.append(tr("ch_error", error=esc(planner.last_error)))
    if planner.write_error:
        lines.append(tr("ch_write_error", error=esc(planner.write_error), email=esc(service_account_email(settings) or "—")))
    b = InlineKeyboardBuilder()
    if plan is not None:
        now = now_utc()
        sts = await states(session, plan.sheet_id)
        upcoming = [p for p in plan.posts if p.due and p.due > now - GRACE and not p.cancelled and not is_out(p, sts.get(p.no))]
        lines += ["", tr("ch_upcoming")]
        lines += [f"{icon(p, sts.get(p.no), now)} {short(p.due, settings)} · №{esc(p.no)} {esc(p.label[:50])}" for p in upcoming[:UPCOMING]]
        if not upcoming:
            lines.append(tr("ch_no_upcoming"))
        horizon = now + timedelta(days=3)
        missing = [
            p for p in upcoming
            if p.needs_file and not has_file(sts.get(p.no)) and p.due <= horizon and not (sts.get(p.no) and sts[p.no].skipped)
        ]
        if missing:
            lines += ["", tr("ch_missing", list=", ".join(f"№{esc(p.no)} ({short(p.due, settings)})" for p in missing))]
        lines += ["", tr("ch_upload_hint")]
        b.button(text=tr("ch_btn_list"), callback_data=ChannelCb(action="list", page=-1))
    b.button(text=tr("ch_btn_refresh"), callback_data=ChannelCb(action="refresh"))
    if is_admin(user):
        b.button(text=tr("ch_btn_sheet"), callback_data=ChannelCb(action="sheet"))
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


@router.message(Command("channel"))
@router.message(F.text.in_(all_variants("btn_channel")))
async def on_channel(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    text, kb = await home_view(session, settings, tr, user)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(ChannelCb.filter(F.action.in_({"home", "refresh"})))
async def cb_home(cb: CallbackQuery, callback_data: ChannelCb, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    await cb.answer()
    text, kb = await home_view(session, settings, tr, user, force=callback_data.action == "refresh")
    await edit_or_send(cb.message, text, kb, edit=True)


# --- the list and a post's card ----------------------------------------------------------

@router.callback_query(ChannelCb.filter(F.action == "list"))
async def cb_list(cb: CallbackQuery, callback_data: ChannelCb, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    plan = await planner.current(session, settings)
    if plan is None:
        await cb.answer(tr("ch_no_plan"), show_alert=True)
        return
    now = now_utc()
    pages = max((len(plan.posts) + PAGE - 1) // PAGE, 1)
    page = callback_data.page
    if page < 0:  # open at the first post still ahead
        ahead = next((i for i, p in enumerate(plan.posts) if p.due and p.due > now - GRACE), len(plan.posts) - 1)
        page = max(ahead, 0) // PAGE
    page = min(max(page, 0), pages - 1)
    sts = await states(session, plan.sheet_id)
    b = InlineKeyboardBuilder()
    for p in plan.posts[page * PAGE:(page + 1) * PAGE]:
        label = f"{icon(p, sts.get(p.no), now)} {short(p.due, settings)} №{p.no} {p.label}"
        b.button(text=label[:64], callback_data=ChannelCb(action="post", no=p.no, page=page))
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(("◀️", page - 1))
    if page < pages - 1:
        nav.append(("▶️", page + 1))
    for text, target in nav:
        b.button(text=text, callback_data=ChannelCb(action="list", page=target))
    if nav:
        b.adjust(*([1] * min(PAGE, len(plan.posts) - page * PAGE)), len(nav))
    b.row(*nav_back(tr))
    await cb.answer()
    await edit_or_send(cb.message, tr("ch_list_title", page=page + 1, pages=pages), b.as_markup(), edit=True)


def card_text(tr: Translator, settings: Settings, plan: Plan, post: PlanPost, st: ChannelPost | None, channel: ChannelInfo | None) -> str:
    lines = [
        tr("ch_card", no=esc(post.no), title=esc(post.label), url=esc(plan.link(post))),
        "🗓 " + (fmt_dt(post.due, settings.tz, tr.lang) if post.due else tr("ch_no_time")),
        tr("ch_card_meta", format=esc(post.format or "—"), owner=esc(post.owner or "—"), status=esc(post.status or "—")),
    ]
    if post.kind == MANUAL:
        lines.append(tr("ch_manual"))
    elif post.needs_file:
        if has_file(st):
            lines.append(tr("ch_file_ok", name=esc(st.file_name or st.file_kind), by=esc(st.file_by or "—"),
                            at=fmt_dt(st.file_at, settings.tz, tr.lang, with_weekday=False) if st.file_at else "—"))
        else:
            lines.append(tr("ch_file_missing", what=what(post.kind, tr.lang), no=esc(post.no)))
    now = now_utc()
    if st is not None and st.posted_at is not None:
        at = fmt_dt(st.posted_at, settings.tz, tr.lang, with_weekday=False)
        by = st.posted_by or tr("ch_by_schedule")
        if st.message_id and channel is not None and st.channel_id == channel.chat_id:
            lines.append(tr("ch_state_posted_link", at=at, by=esc(by), url=esc(channel.post_url(st.message_id))))
        else:
            lines.append(tr("ch_state_posted", at=at, by=esc(by)))
    elif post.posted:
        lines.append(tr("ch_state_posted_sheet"))
    elif post.cancelled:
        lines.append(tr("ch_state_cancelled"))
    elif st is not None and st.skipped:
        lines.append(tr("ch_state_skipped"))
    elif post.kind != MANUAL and post.due is not None and post.due < now - GRACE:
        lines.append(tr("ch_state_late"))
    elif post.kind != MANUAL:
        lines.append(tr("ch_state_planned", channel=esc(channel_name(channel)) if channel else tr("ch_channel_none")))
    if st is not None and st.error and st.posted_at is None:
        lines.append(tr("ch_state_error", error=esc(st.error)))
    body = post.text.strip()
    if body:
        preview = body if len(body) <= 700 else body[:700].rstrip() + "…"
        lines += ["", tr("ch_text_head"), esc(preview)]
    elif post.kind != MANUAL:
        lines += ["", tr("ch_text_empty")]
    return "\n".join(lines)


def card_kb(tr: Translator, post: PlanPost, st: ChannelPost | None, page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    out = is_out(post, st)
    if post.kind != MANUAL:
        b.button(text=tr("ch_btn_preview"), callback_data=ChannelCb(action="preview", no=post.no, page=page))
        if not out and not post.cancelled:
            b.button(text=tr("ch_btn_publish"), callback_data=ChannelCb(action="pub", no=post.no, page=page))
    if not out and not post.cancelled:
        if st is not None and st.skipped:
            b.button(text=tr("ch_btn_unskip"), callback_data=ChannelCb(action="unskip", no=post.no, page=page))
        elif post.kind != MANUAL:
            b.button(text=tr("ch_btn_skip"), callback_data=ChannelCb(action="skip", no=post.no, page=page))
        if has_file(st):
            b.button(text=tr("ch_btn_unfile"), callback_data=ChannelCb(action="unfile", no=post.no, page=page))
        if post.kind == MANUAL:
            b.button(text=tr("ch_btn_done"), callback_data=ChannelCb(action="done", no=post.no, page=page))
    b.adjust(1)
    b.row(*nav_back(tr, "list", page))
    return b.as_markup()


async def show_card(target: Message, tr: Translator, session: AsyncSession, settings: Settings, plan: Plan, post: PlanPost, page: int, *, edit: bool) -> None:
    st = await post_state(session, plan.sheet_id, post.no)
    channel = await app_settings.get_content_channel(session, settings)
    await edit_or_send(target, card_text(tr, settings, plan, post, st, channel), card_kb(tr, post, st, page), edit=edit)


async def find_post(cb: CallbackQuery, tr: Translator, session: AsyncSession, settings: Settings, no: str) -> tuple[Plan, PlanPost] | None:
    plan = await planner.current(session, settings)
    post = plan.by_no.get(no) if plan else None
    if post is None:
        await cb.answer(tr("ch_no_post", no=no), show_alert=True)
        return None
    return plan, post


@router.callback_query(ChannelCb.filter(F.action == "post"))
async def cb_post(cb: CallbackQuery, callback_data: ChannelCb, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    found = await find_post(cb, tr, session, settings, callback_data.no)
    if found is None:
        return
    await cb.answer()
    await show_card(cb.message, tr, session, settings, *found, callback_data.page, edit=True)


@router.callback_query(ChannelCb.filter(F.action == "preview"))
async def cb_preview(cb: CallbackQuery, callback_data: ChannelCb, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    found = await find_post(cb, tr, session, settings, callback_data.no)
    if found is None:
        return
    plan, post = found
    st = await post_state(session, plan.sheet_id, post.no)
    await cb.answer()
    await cb.message.answer(tr("ch_preview_head"))
    if post.needs_file and not has_file(st):
        await cb.message.answer(tr("ch_preview_no_file", what=what(post.kind, tr.lang)))
    sent = await send_post(bot, cb.message.chat.id, post, st.file_id if st else None, st.file_kind if st else None)
    if sent is None:
        await cb.message.answer(tr("ch_text_empty"))


@router.callback_query(ChannelCb.filter(F.action == "pub"))
async def cb_publish_ask(cb: CallbackQuery, callback_data: ChannelCb, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    found = await find_post(cb, tr, session, settings, callback_data.no)
    if found is None:
        return
    channel = await app_settings.get_content_channel(session, settings)
    if channel is None:
        await cb.answer(tr("ch_no_channel"), show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text=tr("ch_btn_publish_yes"), callback_data=ChannelCb(action="pub_go", no=callback_data.no, page=callback_data.page))
    b.button(text=tr("btn_back"), callback_data=ChannelCb(action="post", no=callback_data.no, page=callback_data.page))
    b.adjust(1)
    await cb.answer()
    await edit_or_send(cb.message, tr("ch_confirm_publish", no=esc(callback_data.no), title=esc(found[1].label), channel=esc(channel_name(channel))), b.as_markup(), edit=True)


@router.callback_query(ChannelCb.filter(F.action == "pub_go"))
async def cb_publish(cb: CallbackQuery, callback_data: ChannelCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    found = await find_post(cb, tr, session, settings, callback_data.no)
    if found is None:
        return
    plan, post = found
    outcome = await publish(bot, session, settings, plan, post, by=user.full_name)
    if outcome.ok:
        await cb.answer(tr("ch_published", no=post.no), show_alert=True)
    elif outcome.key == "failed":
        await cb.answer(tr("ch_publish_failed", no=post.no, error=outcome.error[:150]), show_alert=True)
    else:
        await cb.answer(tr(f"ch_why_{outcome.key}", what=what(post.kind, tr.lang)), show_alert=True)
    await show_card(cb.message, tr, session, settings, plan, post, callback_data.page, edit=True)


@router.callback_query(ChannelCb.filter(F.action.in_({"skip", "unskip", "unfile", "done"})))
async def cb_change(cb: CallbackQuery, callback_data: ChannelCb, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    found = await find_post(cb, tr, session, settings, callback_data.no)
    if found is None:
        return
    plan, post = found
    action = callback_data.action
    if action in ("skip", "unskip"):
        st = await post_state(session, plan.sheet_id, post.no, create=True)
        st.skipped = action == "skip"
        await session.commit()
    elif action == "unfile":
        await detach(session, plan, post)
    else:
        await mark_done_by_hand(session, plan, post, user.full_name)
    await cb.answer(tr(f"ch_done_{action}", no=post.no))
    await show_card(cb.message, tr, session, settings, plan, post, callback_data.page, edit=True)


# --- files from the content team ---------------------------------------------------------

@router.message(StateFilter(None), F.photo | F.document)
async def on_file(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    plan = await planner.current(session, settings)
    if plan is None:
        await message.answer(tr("ch_no_plan"))
        return
    upload = file_of(message)
    no = number_in(message.caption)
    if no is not None:
        post = plan.by_no.get(no)
        if post is None:
            await message.answer(tr("ch_no_post", no=no))
            return
        await attach_and_report(message, user, tr, session, settings, bot, plan, post, upload)
        return
    now = now_utc()
    sts = await states(session, plan.sheet_id)
    open_posts = [
        p for p in plan.posts
        if fits(p, upload["kind"]) and p.due and not p.cancelled and not is_out(p, sts.get(p.no)) and not (sts.get(p.no) and sts[p.no].skipped)
    ]
    ahead = [p for p in open_posts if p.due > now - GRACE]
    past = [p for p in reversed(open_posts) if p.due <= now - GRACE]
    candidates = (ahead + past)[:PICK]
    kind_name = what(PHOTO if upload["kind"] in ("photo", "image") else DOCUMENT, tr.lang)
    if not candidates:
        await message.answer(tr("ch_no_candidates", what=kind_name))
        return
    await state.update_data(channel_upload=upload)
    b = InlineKeyboardBuilder()
    for p in candidates:
        mark = "🔁" if has_file(sts.get(p.no)) else "❗"
        b.button(text=f"{mark} №{p.no} · {short(p.due, settings)} · {p.label}"[:64], callback_data=ChannelCb(action="attach", no=p.no))
    b.adjust(1)
    await message.answer(tr("ch_pick_post", what=kind_name), reply_markup=b.as_markup())


@router.callback_query(ChannelCb.filter(F.action == "attach"))
async def cb_attach(cb: CallbackQuery, callback_data: ChannelCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    upload = (await state.get_data()).get("channel_upload")
    found = await find_post(cb, tr, session, settings, callback_data.no)
    if found is None:
        return
    if not upload:
        await cb.answer(tr("stale_button"), show_alert=True)
        return
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if await attach_and_report(cb.message, user, tr, session, settings, bot, *found, upload):
        await state.update_data(channel_upload=None)


async def attach_and_report(
    message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot, plan: Plan, post: PlanPost, upload: dict
) -> bool:
    """Attach the file and show the post as it will look; False if the file does not fit."""
    if not post.needs_file:
        await message.answer(tr("ch_not_media", no=esc(post.no), format=esc(post.format or "—")))
        return False
    if not fits(post, upload["kind"]):
        await message.answer(tr("ch_wrong_kind", no=esc(post.no), format=esc(post.format), what=what(post.kind, tr.lang)))
        return False
    st = await post_state(session, plan.sheet_id, post.no)
    if is_out(post, st):
        await message.answer(tr("ch_post_done", no=esc(post.no)))
        return False
    chat_id = message.chat.id
    file_id, file_kind = upload["file_id"], "photo" if post.kind == PHOTO else "document"
    await message.answer(tr("ch_preview_head"))
    if upload["kind"] == "image":
        # A banner sent as a file: upload it again as a photo, which is also the preview.
        try:
            data = await bot.download(file_id)
            sent = await send_post(bot, chat_id, post, BufferedInputFile(data.getvalue(), filename=upload["name"] or "banner.png"), "photo")
            file_id = sent.photo[-1].file_id
        except Exception as e:  # noqa: BLE001 - too big, not an image Telegram accepts...
            await message.answer(tr("ch_image_failed", error=esc(str(e)[:200])))
            return False
    else:
        await send_post(bot, chat_id, post, file_id, file_kind)
    replaced = await attach_file(session, plan, post, file_id=file_id, file_kind=file_kind, name=upload["name"], by=user.full_name)
    channel = await app_settings.get_content_channel(session, settings)
    when = fmt_dt(post.due, settings.tz, tr.lang) if post.due else tr("ch_no_time")
    text = tr("ch_attached_replaced" if replaced else "ch_attached", no=esc(post.no), when=when,
              channel=esc(channel_name(channel)) if channel else tr("ch_channel_none"))
    b = InlineKeyboardBuilder()
    st = await post_state(session, plan.sheet_id, post.no)
    if post.due is not None and post.due <= now_utc() and not (st and st.skipped):
        text += "\n\n" + tr("ch_attached_late")
        b.button(text=tr("ch_btn_publish_yes"), callback_data=ChannelCb(action="pub_go", no=post.no))
    b.button(text=tr("ch_btn_card"), callback_data=ChannelCb(action="post", no=post.no))
    b.adjust(1)
    await message.answer(text, reply_markup=b.as_markup())
    return True


# --- settings: the plan's spreadsheet and the channel --------------------------------------

@router.callback_query(ChannelCb.filter(F.action == "sheet"))
async def cb_sheet(cb: CallbackQuery, user: User, tr: Translator, settings: Settings, state: FSMContext) -> None:
    if not is_admin(user):
        await cb.answer(tr("err_no_rights"), show_alert=True)
        return
    await state.set_state(ChannelSG.link)
    await cb.answer()
    await cb.message.answer(tr("ch_prompt_link", email=esc(service_account_email(settings) or "—")))


@router.message(ChannelSG.link, F.text)
async def on_sheet_link(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    if not is_admin(user):
        await state.clear()
        return
    sheet_id = parse_sheet_id(message.text)
    if sheet_id is None:
        await message.answer(tr("gs_err_link"))
        return
    backend = planner.make_backend(sheet_id, settings)
    try:
        plan = await planner.read(backend, settings)
    except LookupError:
        await message.answer(tr("ch_err_no_plan_sheet"))
        return
    except Exception as e:  # noqa: BLE001 - no access, no key, wrong link
        await message.answer(tr("gs_err_access", error=esc(describe_error(e)), email=esc(service_account_email(settings) or "—")))
        return
    await state.clear()
    await app_settings.set_content_sheet(session, sheet_id)
    await session.commit()
    planner.use(backend, plan)
    await message.answer(tr("ch_sheet_connected", tab=esc(plan.tab), n=len(plan.posts)))
    await alert_admins(bot, session, settings, t(settings.default_lang, "ch_sheet_changed", by=esc(user.full_name), url=esc(backend.url)))
    text, kb = await home_view(session, settings, tr, user)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(ChannelCb.filter(F.action == "use"))
async def cb_use_channel(cb: CallbackQuery, callback_data: ChannelCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    if user.tg_id not in settings.admin_ids:  # what the school publishes belongs to the super-admins
        await cb.answer(tr("err_super_only"), show_alert=True)
        return
    chat_id = int(callback_data.no)
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError as e:
        await cb.answer(tr("adm_modchat_test_fail", error=str(e)[:150]), show_alert=True)
        return
    if chat.type != "channel" or me.status != "administrator" or not getattr(me, "can_post_messages", False):
        await cb.answer(tr("ch_channel_err_rights"), show_alert=True)
        return
    channel = ChannelInfo(chat_id, chat.title, chat.username)
    await app_settings.set_content_channel(session, channel)
    await session.commit()
    await cb.answer(tr("saved"))
    await edit_or_send(cb.message, tr("ch_channel_set", channel=esc(channel_name(channel))), None, edit=True)
    await alert_admins(bot, session, settings, t(settings.default_lang, "ch_channel_changed", by=esc(user.full_name), channel=esc(channel_name(channel))))
