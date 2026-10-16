"""Status buttons on lead cards. Allowed for anyone in the leads chat (a private group of
the sales team) and for bot admins, who also get cards in DMs when no chat is set."""

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import SalesCb
from app.config import Settings
from app.db.models import User, UserRole
from app.i18n import t
from app.services import app_settings
from app.services import courses as courses_svc
from app.services.lead_cards import card_kb, card_text, refresh_card

router = Router(name="sales")


async def can_work_leads(cb: CallbackQuery, session: AsyncSession, settings: Settings, user: User | None) -> bool:
    if user is not None and user.role == UserRole.ADMIN:
        return True
    chat_id, _ = await app_settings.get_leads_chat(session, settings)
    return chat_id is not None and cb.message is not None and cb.message.chat.id == chat_id


@router.callback_query(SalesCb.filter())
async def on_sales_button(cb: CallbackQuery, callback_data: SalesCb, session: AsyncSession, settings: Settings, bot: Bot, user: User | None = None) -> None:
    lang = settings.default_lang
    if not await can_work_leads(cb, session, settings, user):
        await cb.answer(t(lang, "err_no_rights"), show_alert=True)
        return
    manager = cb.from_user.full_name + (f" (@{cb.from_user.username})" if cb.from_user.username else "")
    result = await courses_svc.change_lead_status(
        session, lead_id=callback_data.lead_id, action=callback_data.action, manager_tg_id=cb.from_user.id, manager_name=manager
    )
    if result is None:
        await cb.answer(t(lang, "sales_not_found"), show_alert=True)
        return
    lead = result.lead
    if not result.changed:
        await cb.answer(t(lang, "sales_already", by=lead.manager_name or "—"), show_alert=True)
    else:
        await cb.answer(t(lang, "mod_done"))
    try:
        await cb.message.edit_text(card_text(lead, settings), reply_markup=card_kb(lead, lang))
    except TelegramBadRequest:
        pass  # "message is not modified" after a repeated press
    if cb.message.chat.id != lead.card_chat_id:
        await refresh_card(bot, settings, lead)  # pressed in a DM: keep the chat card in sync
