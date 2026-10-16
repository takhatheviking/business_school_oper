"""The lead card that sales see: posted to the leads chat (admins' DMs as a fallback) with
status buttons, and edited in place when the status changes."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import SalesCb
from app.config import Settings
from app.db.models import Lead, LeadStatus
from app.formatting import esc, fmt_dt
from app.i18n import LANG_NAMES, t
from app.services import app_settings
from app.services.notify import admin_tg_ids, safe_send

log = logging.getLogger(__name__)

STATUS_ICON = {LeadStatus.NEW: "🆕", LeadStatus.IN_WORK: "🙋", LeadStatus.ENROLLED: "✅", LeadStatus.REJECTED: "❌"}


def interest(lead: Lead, lang: str) -> str:
    return lead.course.loc("title", lang) if lead.course else t(lang, "lead_consultation")


def card_text(lead: Lead, settings: Settings, lang: str | None = None) -> str:
    lang = lang or settings.default_lang
    user = lead.user
    lines = [
        t(lang, "sales_title", id=lead.id, icon=STATUS_ICON.get(LeadStatus(lead.status), "•"), status=t(lang, f"lead_status_{lead.status}")),
        t(lang, "sales_interest", interest=esc(interest(lead, lang))),
        t(lang, "sales_name", name=esc(lead.name)),
        t(lang, "sales_phone", phone=esc(lead.phone)),
        t(lang, "sales_telegram", username=f"@{esc(user.username)}" if user.username else "—", tg_id=user.tg_id),
        t(lang, "sales_meta", language=LANG_NAMES.get(lead.lang or "", "—"), source=esc(lead.source or "—"), at=fmt_dt(lead.created_at, settings.tz, lang, with_weekday=False)),
    ]
    if lead.comment:
        lines.append(t(lang, "sales_comment", comment=esc(lead.comment)))
    if lead.manager_name and lead.status_changed_at:
        at = fmt_dt(lead.status_changed_at, settings.tz, lang, with_weekday=False)
        lines += ["", t(lang, f"sales_by_{lead.status}", by=esc(lead.manager_name), at=at)]
    return "\n".join(lines)


def card_kb(lead: Lead, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if lead.status == LeadStatus.NEW:
        b.button(text=t(lang, "sales_btn_take"), callback_data=SalesCb(action="take", lead_id=lead.id))
    if lead.status in (LeadStatus.NEW, LeadStatus.IN_WORK):
        b.button(text=t(lang, "sales_btn_enrolled"), callback_data=SalesCb(action="enrolled", lead_id=lead.id))
        b.button(text=t(lang, "sales_btn_rejected"), callback_data=SalesCb(action="rejected", lead_id=lead.id))
    else:
        b.button(text=t(lang, "sales_btn_reopen"), callback_data=SalesCb(action="reopen", lead_id=lead.id))
    if lead.status == LeadStatus.NEW:
        b.adjust(1, 2)
    else:
        b.adjust(2)
    return b.as_markup()


async def post_lead(bot: Bot, session: AsyncSession, settings: Settings, lead: Lead) -> bool:
    """Send a new lead to the leads chat; admins get it in DMs when there is no chat or it fails.
    Commits the card's location. Returns False if nobody got it."""
    lang = settings.default_lang
    text, kb = card_text(lead, settings), card_kb(lead, lang)
    chat_id, _ = await app_settings.get_leads_chat(session, settings)
    if chat_id:
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:  # noqa: BLE001 - the bot was removed, the chat migrated... fall back to DMs
            log.warning("lead %s: leads chat %s failed: %s", lead.id, chat_id, e)
        else:
            lead.card_chat_id, lead.card_message_id = msg.chat.id, msg.message_id
            await session.commit()
            return True
    delivered = False
    for tg_id in await admin_tg_ids(session, settings):
        delivered = await safe_send(bot, session, tg_id, text, reply_markup=kb) or delivered
    return delivered


async def refresh_card(bot: Bot, settings: Settings, lead: Lead) -> None:
    """Update the card in the leads chat after a status change made elsewhere (e.g. from an admin's DM)."""
    if not (lead.card_chat_id and lead.card_message_id):
        return
    try:
        await bot.edit_message_text(
            card_text(lead, settings), chat_id=lead.card_chat_id, message_id=lead.card_message_id, reply_markup=card_kb(lead, settings.default_lang)
        )
    except TelegramBadRequest as e:  # deleted, or "message is not modified"
        log.info("lead %s: card not updated: %s", lead.id, e)
