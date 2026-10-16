"""Admin: open leads (with the same status buttons sales have) and the leads chat."""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.settings import back_kb, is_super
from app.bot.keyboards import admin_back
from app.config import Settings
from app.db.models import Lead, LeadStatus, User
from app.formatting import esc
from app.i18n import Translator, t
from app.services import app_settings
from app.services import courses as courses_svc
from app.services.lead_cards import STATUS_ICON, card_kb, card_text, interest
from app.services.notify import alert_admins

router = Router(name="admin_leads")


# --- Open leads --------------------------------------------------------------------------

async def leads_view(session: AsyncSession, tr: Translator) -> tuple[str, InlineKeyboardMarkup]:
    leads, total = await courses_svc.open_leads(session)
    counts = await courses_svc.lead_counts(session)
    b = InlineKeyboardBuilder()
    for lead in leads:
        icon = STATUS_ICON.get(LeadStatus(lead.status), "•")
        b.button(text=f"{icon} №{lead.id} · {lead.name} · {interest(lead, tr.lang)}"[:64], callback_data=AdminCb(action="lead", item_id=lead.id))
    b.adjust(1)
    b.row(admin_back(tr))
    text = tr(
        "adm_leads_title",
        open=total,
        new=counts[LeadStatus.NEW],
        in_work=counts[LeadStatus.IN_WORK],
        enrolled=counts[LeadStatus.ENROLLED],
        rejected=counts[LeadStatus.REJECTED],
    )
    if not total:
        text += "\n\n" + tr("adm_leads_empty")
    return text, b.as_markup()


@router.callback_query(AdminCb.filter(F.action == "leads"))
async def cb_leads(cb: CallbackQuery, tr: Translator, session: AsyncSession) -> None:
    text, kb = await leads_view(session, tr)
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(AdminCb.filter(F.action == "lead"))
async def cb_lead(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    lead = await session.get(Lead, callback_data.item_id)
    if lead is None:
        await cb.answer(tr("sales_not_found"), show_alert=True)
        return
    await cb.answer()
    # A separate message: its status buttons work like the card in the leads chat.
    await cb.message.answer(card_text(lead, settings), reply_markup=card_kb(lead, settings.default_lang))


# --- Leads chat --------------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "leadchat"))
async def cb_leadchat(cb: CallbackQuery, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    chat_id, title = await app_settings.get_leads_chat(session, settings)
    text = tr("adm_leadchat_current", title=esc(title or str(chat_id))) if chat_id else tr("adm_leadchat_none")
    extra = [(tr("adm_btn_modchat_test"), AdminCb(action="leadchat_test"))] if chat_id else []
    await cb.answer()
    await cb.message.edit_text(text + "\n\n" + tr("adm_leadchat_howto"), reply_markup=back_kb(tr, *extra))


@router.callback_query(AdminCb.filter(F.action == "leadchat_test"))
async def cb_leadchat_test(cb: CallbackQuery, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    chat_id, _ = await app_settings.get_leads_chat(session, settings)
    try:
        await bot.send_message(chat_id, tr("adm_leadchat_test_msg"))
    except Exception as e:  # noqa: BLE001 - shown to the admin as is
        await cb.answer(tr("adm_modchat_test_fail", error=str(e)[:150]), show_alert=True)
        return
    await cb.answer(tr("adm_leadchat_test_ok"), show_alert=True)


@router.callback_query(AdminCb.filter(F.action == "set_leadchat"))
async def cb_set_leadchat(cb: CallbackQuery, callback_data: AdminCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    if not is_super(user, settings):
        await cb.answer(tr("err_super_only"), show_alert=True)
        return
    chat_id = int(callback_data.value)
    try:
        chat = await bot.get_chat(chat_id)
        presser = await bot.get_chat_member(chat_id, cb.from_user.id)
    except TelegramAPIError as e:
        await cb.answer(tr("adm_modchat_test_fail", error=str(e)[:150]), show_alert=True)
        return
    # Leads carry names and phones: only a private chat that the presser administers.
    if chat.username or presser.status not in ("creator", "administrator"):
        await cb.answer(tr("adm_modchat_err_unsafe"), show_alert=True)
        return
    try:
        await bot.send_message(chat_id, tr("adm_leadchat_test_msg"))
    except TelegramAPIError as e:
        await cb.answer(tr("adm_modchat_test_fail", error=str(e)[:150]), show_alert=True)
        return
    await app_settings.set_leads_chat(session, chat_id, chat.title)
    await session.commit()
    await cb.answer(tr("saved"))
    await cb.message.edit_text(tr("adm_leadchat_current", title=esc(chat.title or str(chat_id))))
    await alert_admins(bot, session, settings, t(settings.default_lang, "adm_leadchat_changed", by=esc(user.full_name), title=esc(chat.title or str(chat_id))))
