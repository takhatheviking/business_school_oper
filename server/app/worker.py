"""Background jobs, run inside the bot process.

Run exactly one bot instance (polling is single-instance anyway): hold expiry uses
SKIP LOCKED, but the delivery and re-post retries assume no second worker."""

import asyncio
import logging
import time
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.keyboards import expired_order_kb, review_kb
from app.config import Settings
from app.db.models import Event, EventStatus, Notification, Ticket, TicketStatus, User
from app.formatting import esc, fmt_dt
from app.i18n import Translator, t
from app.services.booking import expire_due_orders, find_duplicate_orders, now_utc, pending_reviews
from app.services.gsheets import describe_error, sheets
from app.services.task_events import announce
from app.services.tasks import board
from app.services.notify import (
    alert_admins,
    deliver_tickets,
    post_for_review,
    review_caption,
    safe_send,
    undelivered_tickets,
    unposted_payments,
)

log = logging.getLogger(__name__)
TICK_SECONDS = 30
REMINDERS = (("reminder_24h", timedelta(hours=24)), ("reminder_3h", timedelta(hours=3)))


async def expire_holds(bot: Bot, session: AsyncSession, settings: Settings) -> None:
    expired = await expire_due_orders(session)
    await session.commit()
    for order in expired:
        lang = order.user.lang or settings.default_lang
        await safe_send(
            bot,
            session,
            order.user.tg_id,
            t(lang, "booking_expired", order_id=order.id, title=esc(order.event.loc("title", lang))),
            reply_markup=expired_order_kb(Translator(lang), order.id),
        )


async def retry_ticket_delivery(bot: Bot, session: AsyncSession, settings: Settings) -> None:
    tickets = await undelivered_tickets(session, older_than=now_utc() - timedelta(minutes=2))
    if tickets:
        await deliver_tickets(bot, session, settings, tickets)


async def retry_review_posts(bot: Bot, session: AsyncSession, settings: Settings) -> None:
    for payment in await unposted_payments(session, older_than=now_utc() - timedelta(minutes=1)):
        order = payment.order
        duplicates = await find_duplicate_orders(session, payment.proof_file_unique_id, order.id)
        caption = review_caption(order, payment, settings, duplicates, settings.max_proof_attempts)
        await post_for_review(bot, session, settings, payment, caption, review_kb(payment.id, settings.default_lang))


async def alert_stale_reviews(bot: Bot, session: AsyncSession, settings: Settings) -> None:
    """A receipt nobody decided on (message deleted, chat switched, moderators asleep) holds
    seats and a paying customer. Nag admins once per window, pointing at the DB list."""
    window = timedelta(minutes=settings.review_alert_minutes)
    now = now_utc()
    stale = [
        p for p in await pending_reviews(session, older_than=now - window, limit=50)
        if p.alerted_at is None or p.alerted_at < now - window
    ]
    if not stale:
        return
    for payment in stale:
        payment.alerted_at = now
    await session.commit()
    orders = ", ".join(f"№{p.order_id}" for p in stale[:15])
    await alert_admins(bot, session, settings, t(settings.default_lang, "adm_alert_stale_reviews", n=len(stale), orders=orders))


async def sync_sheets(bot: Bot, session: AsyncSession, settings: Settings) -> None:
    if not sheets.enabled(settings) or not sheets.due(settings):
        return
    try:
        await sheets.sync(session, settings)
    except Exception as e:
        sheets.last_error = describe_error(e)
        log.warning("google sheets sync failed: %s", sheets.last_error)
        await session.rollback()
        if sheets.alerted_error != sheets.last_error:  # one alert per distinct problem
            sheets.alerted_error = sheets.last_error
            await alert_admins(bot, session, settings, t(settings.default_lang, "gs_alert_failed", error=esc(sheets.last_error)))
    else:
        sheets.alerted_error = None


async def send_reminders(bot: Bot, session: AsyncSession, settings: Settings) -> None:
    now = now_utc()
    for kind, before in REMINDERS:
        # Window: the event starts within `before`, but not later than the next, shorter reminder.
        stmt = (
            select(User, Event)
            .join(Ticket, Ticket.user_id == User.id)
            .join(Event, Ticket.event_id == Event.id)
            .where(
                Event.status == EventStatus.PUBLISHED,
                Event.starts_at > now,
                Event.starts_at <= now + before,
                Ticket.status == TicketStatus.VALID,
                User.bot_blocked.is_(False),
            )
            .distinct()
        )
        for user, event in (await session.execute(stmt)).all():
            if kind == "reminder_24h" and event.starts_at <= now + timedelta(hours=3):
                continue  # too late for the day-before reminder; the 3h one covers it
            claimed = await session.execute(
                insert(Notification)
                .values(user_id=user.id, event_id=event.id, kind=kind)
                .on_conflict_do_nothing()
                .returning(Notification.id)
            )
            if claimed.scalar_one_or_none() is None:
                continue
            await session.commit()
            lang = user.lang or settings.default_lang
            place = event.loc("venue", lang) or "—"
            await safe_send(
                bot,
                session,
                user.tg_id,
                t(lang, kind, title=esc(event.loc("title", lang)), when=fmt_dt(event.starts_at, settings.tz, lang), place=esc(place)),
            )
    await session.commit()


HEARTBEAT_SECONDS = 300


async def sync_tasks(bot: Bot, session: AsyncSession, settings: Settings) -> None:
    """Status spreadsheet: find status changes and announce them (app/services/tasks.py).

    The heartbeat to the Apps Script comes first: it brings the spreadsheet's test mode, which
    must be known before anything is sent. The server claims the status events only while
    its polls succeed; otherwise the script goes on sending them itself."""
    if not board.enabled:
        return
    bridge = board.bridge if board.bridge is not None and board.bridge.enabled else None
    now = time.monotonic()

    async def beat(events: bool) -> None:
        board.last_heartbeat = now
        ok, test_chat = await bridge.heartbeat(events=events)
        if ok:
            board.test_chat_id, board.events_claimed = test_chat, events

    if bridge and now - board.last_heartbeat >= HEARTBEAT_SECONDS:
        await beat(board.events_claimed)
    if now - board.last_poll < settings.tasks_poll_seconds:
        return
    board.last_poll = now
    try:
        changes = await board.poll(session)
    except Exception as e:  # noqa: BLE001 - shown to admins once per distinct problem
        error = describe_error(e)
        log.warning("status spreadsheet poll failed: %s", error)
        if board.last_error != error:
            board.last_error = error
            await alert_admins(bot, session, settings, t(settings.default_lang, "tk_alert_failed", error=esc(error)))
        if bridge and board.events_claimed:
            await beat(False)
        return
    board.last_error = None
    if bridge and not board.events_claimed:
        await beat(True)
    for change in changes:
        await announce(bot, session, settings, board, change)


JOBS = (expire_holds, retry_ticket_delivery, retry_review_posts, alert_stale_reviews, send_reminders, sync_sheets, sync_tasks)


async def run_worker(bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    log.info("worker started")
    while True:
        for job in JOBS:
            try:
                async with session_factory() as session:
                    await job(bot, session, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker job %s failed", job.__name__)
        await asyncio.sleep(TICK_SECONDS)
