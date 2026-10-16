"""RACI messages about task status changes: who gets what, in which language, and the
delivery (honouring the spreadsheet's test mode and the «Уведомлять» column)."""

import logging
from dataclasses import dataclass
from datetime import date

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import TaskCb
from app.config import Settings
from app.db.models import User
from app.formatting import esc
from app.i18n import t
from app.services.notify import safe_send
from app.services.status_sheet import CANCELLED, DONE, RISK, WORK, Task
from app.services.tasks import Change, Review, Snapshot, TaskBoard
from app.services.team import Person

log = logging.getLogger(__name__)
TASK_TEXT_MAX = 300


@dataclass
class Outgoing:
    person: Person
    kind: str  # started | done_a | done_i | risk | cancelled | accepted | returned
    task: Task
    by: str = ""
    reason: str = ""


def cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def dm(d: date | None) -> str:
    return f"{d:%d.%m}" if d else "—"


def _keys(people: list[Person]) -> set[str]:
    return {p.key for p in people}


def recipients(snap: Snapshot, ev: Change | Review, *, c_first_start: bool) -> list[Outgoing]:
    """Who hears about a change. c_first_start: the task starts for the first time (C is told once)."""
    task = ev.task
    r = snap.r_of(task)
    a = [p for p in snap.a_of(task) if p.key not in _keys(r)]  # one's own work is not one's to accept
    c, i = snap.c_of(task), snap.i_of(task)
    by = ev.actor_name
    plan: list[tuple[list[Person], str]] = []
    if isinstance(ev, Review):
        plan = [(r, "accepted")]
    elif ev.source == "bot" and ev.prev == DONE and ev.new == WORK:
        plan = [(r, "returned")]
    elif ev.new == WORK and c_first_start:
        plan = [(c, "started")]
    elif ev.new == DONE:
        plan = [(a, "done_a"), (i, "done_i")]
    elif ev.new == RISK:
        plan = [(a, "risk"), (c, "risk")]
    elif ev.new == CANCELLED:
        plan = [(r, "cancelled"), (i, "cancelled")]
    skip = _keys(ev.actor) if ev.actor else set()
    seen: set[str] = set()
    out = []
    for people, kind in plan:
        for p in people:
            if p.key in skip or p.key in seen:
                continue
            seen.add(p.key)
            out.append(Outgoing(p, kind, task, by, getattr(ev, "reason", "")))
    return out


def render(o: Outgoing, snap: Snapshot, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    tk = o.task
    head = t(lang, f"ev_{o.kind}", id=f'<a href="{esc(snap.book.link(tk))}">{esc(tk.id)}</a>')
    lines = [head, esc(cut(tk.task, TASK_TEXT_MAX)), ""]
    lines.append(t(lang, "ev_who_due", r=esc(tk.r or "—"), due=dm(tk.due)))
    if o.kind == "started":
        lines.append(t(lang, "ev_started_hint"))
    if o.kind == "done_a":
        if tk.fact:
            late = (tk.fact - tk.due).days if tk.due and tk.fact > tk.due else 0
            lines.append(t(lang, "ev_fact_late", fact=dm(tk.fact), days=late) if late else t(lang, "ev_fact", fact=dm(tk.fact)))
        if tk.result:
            lines.append(t(lang, "ev_result", result=esc(cut(tk.result, 200))))
    if o.kind in ("risk", "cancelled"):
        lines.append(t(lang, "ev_comment", comment=esc(cut(tk.comment, 400))) if tk.comment else t(lang, "ev_no_comment"))
    if o.kind == "returned" and o.reason:
        lines.append(t(lang, "ev_reason", reason=esc(o.reason)))
    if o.by:
        lines.append(t(lang, "ev_by", by=esc(o.by)))
    b = InlineKeyboardBuilder()
    if o.kind == "done_a":
        b.button(text=t(lang, "tk_btn_accept"), callback_data=TaskCb(action="accept", task_id=tk.id))
        b.button(text=t(lang, "tk_btn_return"), callback_data=TaskCb(action="return", task_id=tk.id))
    b.button(text=t(lang, "tk_btn_open"), callback_data=TaskCb(action="open", task_id=tk.id))
    b.adjust(2, 1) if o.kind == "done_a" else b.adjust(1)
    return "\n".join(lines), b.as_markup()


async def deliver(bot: Bot, session: AsyncSession, settings: Settings, board: TaskBoard, snap: Snapshot, items: list[Outgoing]) -> int:
    """Send the messages; returns how many went out. People without a chat_id (they have not
    pressed /start yet) or with «Уведомлять: Нет» are skipped, as in the Apps Script."""
    if not items:
        return 0
    chat_ids = [int(o.person.chat_id) for o in items if o.person.chat_id.lstrip("-").isdigit()]
    langs = dict((await session.execute(select(User.tg_id, User.lang).where(User.tg_id.in_(chat_ids)))).all()) if chat_ids else {}
    sent = 0
    for o in items:
        if board.test_chat_id:
            chat, lang = board.test_chat_id, settings.default_lang
        elif not o.person.notify or not o.person.chat_id.lstrip("-").isdigit():
            log.info("task %s: %s not notified (%s)", o.task.id, o.person.role, "off" if not o.person.notify else "no chat_id")
            continue
        else:
            chat = int(o.person.chat_id)
            lang = langs.get(chat) or settings.default_lang
        text, kb = render(o, snap, lang)
        if board.test_chat_id:
            text = t(lang, "ev_test_for", who=esc(o.person.label)) + "\n\n" + text
        if await safe_send(bot, session, chat, text, reply_markup=kb, disable_web_page_preview=True):
            sent += 1
    return sent


async def announce(bot: Bot, session: AsyncSession, settings: Settings, board: TaskBoard, ev: Change | Review) -> int:
    snap = await board.get()
    first_start = False
    if isinstance(ev, Change) and ev.new == WORK and not (ev.source == "bot" and ev.prev == DONE):
        first_start = await board.mark_started_notified(session, ev.task.id)
    return await deliver(bot, session, settings, board, snap, recipients(snap, ev, c_first_start=first_start))
