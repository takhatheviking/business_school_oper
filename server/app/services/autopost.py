"""Autoposting of the content plan to the channel (docs/CHANNEL.md).

The worker reads the plan sheet every CONTENT_POLL_SECONDS and, on every tick (30 s):
  - publishes text, banner and PDF posts whose time has come; up to GRACE late too (the bot
    was restarting). A banner or PDF post waits for its file and never goes out without it;
  - reminds the content team about a missing file 3 h and 1 h before, and says at the post's
    time that it did not go out (the file can still come: the bot then offers to publish);
  - gives a 15-minute heads-up with the text for posts people publish by hand (round videos);
  - lists once the posts that missed their time by more than GRACE (bot down, no channel).

The content team: users with the "content" role plus the super-admins (ADMIN_IDS).
What the sheet does not hold lives in channel_posts. A post goes out once: its row is claimed
in the DB before sending, because the worker and a "Publish now" press run side by side.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import ChannelCb
from app.config import Settings
from app.db.models import ChannelPost, User, UserRole
from app.formatting import esc
from app.i18n import t
from app.services import app_settings
from app.services.app_settings import ChannelInfo
from app.services.booking import now_utc
from app.services.content_plan import (
    DOCUMENT,
    MANUAL,
    PHOTO,
    PLANNED,
    POSTED,
    READY,
    TEXT,
    GspreadPlanBackend,
    Plan,
    PlanBackend,
    PlanPost,
    norm,
    parse_posts,
)
from app.services.gsheets import describe_error
from app.services.notify import safe_send

log = logging.getLogger(__name__)
GRACE = timedelta(minutes=20)  # a post this late still goes out on its own
LATE_WINDOW = timedelta(hours=12)  # misses older than this are history, not news
REMIND_3H, REMIND_1H = timedelta(hours=3), timedelta(hours=1)
MANUAL_HEADS_UP = timedelta(minutes=15)
CLAIM_TTL = timedelta(minutes=5)  # a claim older than this is a crash mid-send, not a send in progress
TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024
KIND_ICON = {TEXT: "📝", PHOTO: "🖼", DOCUMENT: "📎", MANUAL: "🎥"}


def tg_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def _head(text: str, limit: int) -> str:
    n = size = 0
    for ch in text:
        size += 2 if ord(ch) > 0xFFFF else 1
        if size > limit:
            break
        n += 1
    head = text[:n]
    for sep in ("\n\n", "\n", " "):
        cut = head.rfind(sep)
        if cut > limit // 2:
            return head[:cut]
    return head


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Pieces of at most `limit`, cut at a paragraph, a line or a space when possible."""
    pieces, rest = [], text.strip()
    while tg_len(rest) > limit:
        head = _head(rest, limit)
        pieces.append(head.rstrip())
        rest = rest[len(head):].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def default_backend(sheet_id: str, settings: Settings) -> PlanBackend:
    return GspreadPlanBackend(sheet_id, settings.google_service_account_file)


class Planner:
    """The plan as last read, plus the reading and writing troubles to show and alert about."""

    def __init__(self) -> None:
        self.backend: PlanBackend | None = None
        self.plan: Plan | None = None
        self.read_at: datetime | None = None
        self.last_poll = float("-inf")
        self.just_read = False  # the worker retries status writes once per fresh read
        self.last_error: str | None = None  # reading the sheet
        self.alerted_error: str | None = None
        self.write_error: str | None = None  # writing statuses back
        self.alerted_write_error: str | None = None
        self.make_backend: Callable[[str, Settings], PlanBackend] = default_backend  # tests put a fake here
        self.lock = asyncio.Lock()

    def reset(self) -> None:
        self.__init__()

    async def backend_for(self, session: AsyncSession, settings: Settings) -> PlanBackend | None:
        sheet_id = await app_settings.get_content_sheet(session, settings)
        if not sheet_id:
            self.backend = self.plan = None
            return None
        if self.backend is None or self.backend.sheet_id != sheet_id:
            self.use(self.make_backend(sheet_id, settings))
        return self.backend

    def use(self, backend: PlanBackend, plan: Plan | None = None) -> None:
        self.backend, self.plan, self.last_error, self.write_error = backend, plan, None, None
        self.last_poll = time.monotonic() if plan else float("-inf")
        self.read_at = now_utc() if plan else None

    async def read(self, backend: PlanBackend, settings: Settings) -> Plan:
        tab, gid, values = await backend.read()
        return Plan(backend.sheet_id, tab, gid, backend.url, parse_posts(values, settings.tz))

    async def refresh(self, session: AsyncSession, settings: Settings, *, force: bool = False) -> Plan | None:
        """The plan, read again if older than CONTENT_POLL_SECONDS. Raises on a failed read
        (the last good plan stays in self.plan)."""
        backend = await self.backend_for(session, settings)
        if backend is None:
            return None
        if not force and self.plan is not None and time.monotonic() - self.last_poll < settings.content_poll_seconds:
            return self.plan
        async with self.lock:
            self.last_poll = time.monotonic()
            try:
                plan = await self.read(backend, settings)
            except Exception as e:
                self.last_error = describe_error(e)
                raise
            self.plan, self.read_at, self.last_error, self.just_read = plan, now_utc(), None, True
        return self.plan

    async def current(self, session: AsyncSession, settings: Settings) -> Plan | None:
        """For screens: a fresh enough plan, or the last good one if the sheet cannot be read."""
        try:
            return await self.refresh(session, settings)
        except Exception:
            log.warning("content plan read failed: %s", self.last_error)
            return self.plan


planner = Planner()


# --- state in the DB --------------------------------------------------------------------

async def states(session: AsyncSession, sheet_id: str) -> dict[str, ChannelPost]:
    rows = await session.execute(
        select(ChannelPost).where(ChannelPost.sheet_id == sheet_id).execution_options(populate_existing=True)
    )
    return {st.post_no: st for st in rows.scalars()}


async def state(session: AsyncSession, sheet_id: str, no: str, *, create: bool = False) -> ChannelPost | None:
    if create:
        await session.execute(
            insert(ChannelPost).values(sheet_id=sheet_id, post_no=no, notified=[]).on_conflict_do_nothing(index_elements=["sheet_id", "post_no"])
        )
    stmt = select(ChannelPost).where(ChannelPost.sheet_id == sheet_id, ChannelPost.post_no == no)
    return (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def note(session: AsyncSession, sheet_id: str, no: str, key: str) -> None:
    """Remember a one-off message about a post (committed before it is sent: at most once)."""
    st = await state(session, sheet_id, no, create=True)
    if key not in st.notified:
        st.notified = [*st.notified, key]
    await session.commit()


def has_file(st: ChannelPost | None) -> bool:
    return st is not None and st.file_id is not None


def is_out(post: PlanPost, st: ChannelPost | None) -> bool:
    return post.posted or (st is not None and st.posted_at is not None)


def icon(post: PlanPost, st: ChannelPost | None, now: datetime) -> str:
    if is_out(post, st):
        return "✅"
    if post.cancelled:
        return "🚫"
    if st is not None and st.skipped:
        return "⏸"
    if post.kind == MANUAL:
        return "🎥"
    if post.due is not None and post.due < now - GRACE:
        return "⌛"
    if post.needs_file and not has_file(st):
        return "❗"
    return KIND_ICON[post.kind]


def short(dt: datetime | None, settings: Settings) -> str:
    return f"{dt.astimezone(settings.tz):%d.%m %H:%M}" if dt else "—"


def what(kind: str, lang: str) -> str:
    return t(lang, "ch_what_photo" if kind == PHOTO else "ch_what_document")


# --- sending ----------------------------------------------------------------------------

async def send_post(bot: Bot, chat_id: int, post: PlanPost, file: str | InputFile | None, file_kind: str | None) -> Message | None:
    """The post as plain text (no HTML: the sheet's text goes out exactly as written).
    A caption longer than Telegram allows goes as a separate message after the file.
    Returns the first message, or None if there was nothing to send."""
    text = post.text.strip()
    first: Message | None = None
    if file is not None and post.needs_file:
        caption = text if tg_len(text) <= CAPTION_LIMIT else ""
        if file_kind == "photo":
            first = await bot.send_photo(chat_id, file, caption=caption or None, parse_mode=None)
        else:
            first = await bot.send_document(chat_id, file, caption=caption or None, parse_mode=None)
        if caption:
            return first
    for piece in split_text(text):
        sent = await bot.send_message(chat_id, piece, parse_mode=None)
        first = first or sent
    return first


@dataclass
class Outcome:
    ok: bool
    key: str  # posted | busy | no_channel | no_file | empty | manual | failed
    error: str = ""
    url: str = ""


async def publish(bot: Bot, session: AsyncSession, settings: Settings, plan: Plan, post: PlanPost, *, by: str | None) -> Outcome:
    channel = await app_settings.get_content_channel(session, settings)
    if channel is None:
        return Outcome(False, "no_channel")
    if post.kind == MANUAL:
        return Outcome(False, "manual")
    if not post.text.strip() and post.kind == TEXT:
        return Outcome(False, "empty")
    st = await state(session, plan.sheet_id, post.no, create=True)
    await session.commit()
    if is_out(post, st):
        return Outcome(False, "posted")
    if post.needs_file and not has_file(st):
        return Outcome(False, "no_file")
    now = now_utc()
    free = ChannelPost.claimed_at.is_(None)
    if by is not None:
        # A claim this old means the bot died while sending: the post may be in the channel.
        # The schedule never sends it again (tick() asks the team to check); a person who
        # looked at the channel and pressed "Publish now" may.
        free = or_(free, ChannelPost.claimed_at < now - CLAIM_TTL)
    claimed = await session.execute(
        update(ChannelPost)
        .where(ChannelPost.id == st.id, ChannelPost.posted_at.is_(None), free)
        .values(claimed_at=now)
        .returning(ChannelPost.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.commit()
    if not won:
        return Outcome(False, "busy")
    try:
        message = await send_post(bot, channel.chat_id, post, st.file_id, st.file_kind)
    except Exception as e:  # noqa: BLE001 - any failure is reported to the team as is
        error = str(e)[:300] or type(e).__name__
        log.warning("posting %s to the channel failed: %s", post.no, error)
        await session.execute(
            update(ChannelPost).where(ChannelPost.id == st.id).values(claimed_at=None, error=error).execution_options(synchronize_session=False)
        )
        await session.commit()
        return Outcome(False, "failed", error)
    await session.execute(
        update(ChannelPost)
        .where(ChannelPost.id == st.id)
        .values(claimed_at=None, posted_at=now_utc(), posted_by=by, channel_id=channel.chat_id, message_id=message.message_id, error=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await mark_sheet(session, plan, post, POSTED)
    return Outcome(True, "posted", url=channel.post_url(message.message_id))


async def mark_sheet(session: AsyncSession, plan: Plan, post: PlanPost, status: str) -> bool:
    """Best effort: a failed write is shown on «📣 Канал» and, for «Joylandi», retried."""
    backend = planner.backend
    if backend is None or backend.sheet_id != plan.sheet_id:
        return False
    try:
        written = await backend.write_status(plan.gid, post.no, status)
    except Exception as e:  # noqa: BLE001
        planner.write_error = describe_error(e)
        log.warning("writing status of post %s failed: %s", post.no, planner.write_error)
        return False
    planner.write_error = None
    if written:
        post.status = status
        if status == POSTED:
            await session.execute(
                update(ChannelPost)
                .where(ChannelPost.sheet_id == plan.sheet_id, ChannelPost.post_no == post.no)
                .values(sheet_marked=True)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
    return written


async def attach(session: AsyncSession, plan: Plan, post: PlanPost, *, file_id: str, file_kind: str, name: str | None, by: str) -> bool:
    """Attach the file to a post; True if it replaced another one. «Rejada» becomes «Tayyor»."""
    st = await state(session, plan.sheet_id, post.no, create=True)
    replaced = st.file_id is not None
    st.file_id, st.file_kind, st.file_name, st.file_by, st.file_at = file_id, file_kind, (name or "")[:255] or None, by[:128], now_utc()
    st.error = None
    await session.commit()
    if norm(post.status) == norm(PLANNED):
        await mark_sheet(session, plan, post, READY)
    return replaced


async def detach(session: AsyncSession, plan: Plan, post: PlanPost) -> None:
    st = await state(session, plan.sheet_id, post.no)
    if st is None:
        return
    st.file_id = st.file_kind = st.file_name = st.file_by = st.file_at = None
    await session.commit()
    if norm(post.status) == norm(READY):
        await mark_sheet(session, plan, post, PLANNED)


async def mark_done_by_hand(session: AsyncSession, plan: Plan, post: PlanPost, by: str) -> None:
    st = await state(session, plan.sheet_id, post.no, create=True)
    if st.posted_at is None:
        st.posted_at, st.posted_by, st.channel_id, st.message_id = now_utc(), by[:128], None, None
        await session.commit()
    await mark_sheet(session, plan, post, POSTED)


# --- the content team -------------------------------------------------------------------

async def content_team(session: AsyncSession, settings: Settings) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(User.tg_id, User.lang).where(or_(User.role == UserRole.CONTENT, User.tg_id.in_(settings.admin_ids)), User.bot_blocked.is_(False))
        )
    ).all()
    langs = {tg_id: lang for tg_id, lang in rows}
    ids = dict.fromkeys([*settings.admin_ids, *langs])
    return [(tg_id, langs.get(tg_id) or settings.default_lang) for tg_id in ids]


Render = Callable[[str], tuple[str, InlineKeyboardMarkup | None]]


async def tell_team(bot: Bot, session: AsyncSession, settings: Settings, render: Render, plain: str | None = None) -> None:
    """A message to every member of the content team in their language; `plain` follows it as
    unformatted text (a post's text, ready to copy)."""
    for tg_id, lang in await content_team(session, settings):
        text, kb = render(lang)
        if await safe_send(bot, session, tg_id, text, reply_markup=kb, disable_web_page_preview=True) and plain:
            for piece in split_text(plain):
                await safe_send(bot, session, tg_id, piece, parse_mode=None)


def card_button(lang: str, no: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "ch_btn_card"), callback_data=ChannelCb(action="post", no=no))
    return b.as_markup()


def headline(post: PlanPost, settings: Settings) -> dict[str, str]:
    return {"no": esc(post.no), "title": esc(post.label), "time": short(post.due, settings)}


# --- the worker -------------------------------------------------------------------------

async def tick(bot: Bot, session: AsyncSession, settings: Settings, now: datetime | None = None) -> None:
    try:
        plan = await planner.refresh(session, settings)
    except Exception:
        error = planner.last_error
        if planner.alerted_error != error:  # one alert per distinct problem
            planner.alerted_error = error
            await tell_team(bot, session, settings, lambda lang: (t(lang, "ch_alert_read_failed", error=esc(error)), None))
        return
    if planner.last_error is None:
        planner.alerted_error = None
    if plan is None:
        return
    now = now or now_utc()
    channel = await app_settings.get_content_channel(session, settings)
    sts = await states(session, plan.sheet_id)
    late: list[PlanPost] = []
    for post in plan.posts:
        st = sts.get(post.no)
        if post.due is None or post.cancelled or is_out(post, st) or (st is not None and st.skipped):
            continue
        lead, done = post.due - now, set(st.notified) if st else set()
        if post.kind == MANUAL:
            if -GRACE < lead <= MANUAL_HEADS_UP and "manual" not in done:
                await heads_up(bot, session, settings, plan, post)
            continue
        if lead > timedelta(0):
            if post.needs_file and not has_file(st):
                key = "1h" if lead <= REMIND_1H else "3h" if lead <= REMIND_3H else None
                if key and key not in done:
                    await note(session, plan.sheet_id, post.no, key)
                    await tell_team(
                        bot, session, settings,
                        lambda lang: (t(lang, "ch_remind", what=what(post.kind, lang), **headline(post, settings)), card_button(lang, post.no)),
                    )
            continue
        if -lead > GRACE:
            if post.due >= now - LATE_WINDOW and not done & {"due", "late"}:
                late.append(post)
            continue
        # Its time has come.
        error = ""
        if channel is None:
            reason = "no_channel"
        elif post.needs_file and not has_file(st):
            reason = "no_file"
        elif st is not None and st.claimed_at is not None and st.claimed_at < now_utc() - CLAIM_TTL:
            reason = "unsure"  # the bot died while sending it: maybe it is out, maybe not
        else:
            outcome = await publish(bot, session, settings, plan, post, by=None)
            if outcome.ok:
                await tell_team(
                    bot, session, settings,
                    lambda lang: (t(lang, "ch_posted_note", url=esc(outcome.url), **headline(post, settings)), None),
                )
                continue
            if outcome.key in ("busy", "posted"):
                continue
            reason, error = outcome.key, outcome.error  # empty | failed: retried every tick within GRACE
        if "due" not in done:
            await note(session, plan.sheet_id, post.no, "due")
            await tell_team(
                bot, session, settings,
                lambda lang: (
                    t(lang, f"ch_alert_{reason}", what=what(post.kind, lang), error=esc(error), **headline(post, settings)),
                    card_button(lang, post.no),
                ),
            )
    if late:
        for post in late:
            await note(session, plan.sheet_id, post.no, "late")
        lines = "\n".join(f"• №{esc(p.no)} · {short(p.due, settings)} · {esc(p.label[:60])}" for p in late)
        await tell_team(bot, session, settings, lambda lang: (t(lang, "ch_alert_late", list=lines), None))
    if planner.just_read:
        planner.just_read = False
        await retry_marks(session, plan, sts)
        if planner.write_error and planner.alerted_write_error != planner.write_error:
            planner.alerted_write_error = error = planner.write_error
            await tell_team(bot, session, settings, lambda lang: (t(lang, "ch_alert_write_failed", error=esc(error)), None))
        elif planner.write_error is None:
            planner.alerted_write_error = None


async def retry_marks(session: AsyncSession, plan: Plan, sts: dict[str, ChannelPost]) -> None:
    """«Joylandi» that did not reach the sheet when the post went out."""
    for st in sts.values():
        post = plan.by_no.get(st.post_no)
        if st.posted_at is None or st.sheet_marked or post is None:
            continue
        if post.posted:
            st.sheet_marked = True
            await session.commit()
        elif not await mark_sheet(session, plan, post, POSTED):
            return  # the sheet does not take writes now; next read tries again


async def heads_up(bot: Bot, session: AsyncSession, settings: Settings, plan: Plan, post: PlanPost) -> None:
    await note(session, plan.sheet_id, post.no, "manual")

    def render(lang: str) -> tuple[str, InlineKeyboardMarkup]:
        b = InlineKeyboardBuilder()
        b.button(text=t(lang, "ch_btn_done"), callback_data=ChannelCb(action="done", no=post.no))
        key = "ch_manual_heads_up" if post.text.strip() else "ch_manual_heads_up_no_text"
        return t(lang, key, format=esc(post.format or "—"), **headline(post, settings)), b.as_markup()

    await tell_team(bot, session, settings, render, plain=post.text.strip() or None)


def channel_name(channel: ChannelInfo | None) -> str:
    if channel is None:
        return ""
    return channel.title or (f"@{channel.username}" if channel.username else str(channel.chat_id))
