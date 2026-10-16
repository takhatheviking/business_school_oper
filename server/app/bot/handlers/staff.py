"""Staff: tasks of the status spreadsheet right in Telegram.

"🗂 Tasks" (or /my) opens: my tasks with status buttons, tasks waiting for my acceptance,
and a summary by department for heads. Changes are written to the spreadsheet and
announced to the RACI of the task (app/services/task_events.py)."""

from collections import defaultdict
from datetime import date

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import TaskCb, TasksCb
from app.bot.states import TaskSG
from app.config import Settings
from app.db.models import User, UserRole
from app.formatting import esc
from app.i18n import Translator, all_variants
from app.services.status_sheet import CANCELLED, DONE, NEW, RISK, WORK, Task
from app.services.task_events import announce, cut, dm
from app.services.tasks import Snapshot, TaskBoard, today_in, week_bounds
from app.services.team import Person

router = Router(name="staff")
router.message.filter(F.chat.type == "private")
PAGE = 10
REASON_MAX = 500
STATUS_ICON = {NEW: "▫️", WORK: "▶️", RISK: "⚠️", DONE: "✅", CANCELLED: "🚫"}
ACTION_STATUS = {"work": WORK, "done": DONE, "risk": RISK, "return": WORK, "cancel": CANCELLED}


def has_team(_: object, team: list[Person] | None = None) -> bool:
    return bool(team)


def is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN


def task_icon(t: Task, today: date) -> str:
    if t.is_open and t.due and t.due < today:
        return "❗"
    return STATUS_ICON.get(t.status, "•")


async def show(target: Message, text: str, kb: InlineKeyboardMarkup, edit: bool) -> None:
    if edit and not (target.photo or target.document):
        await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


def back_home(tr: Translator) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=tr("btn_back"), callback_data=TasksCb(action="home").pack())


# --- Home ---------------------------------------------------------------------------------

async def home_view(board: TaskBoard, session: AsyncSession, team: list[Person], user: User, tr: Translator, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    snap = await board.get()
    today = today_in(settings.tz)
    mine = board.my_tasks(snap, team, today)
    review = board.to_review(snap, team, await board.accepted_ids(session))
    depts = board.visible_depts(snap, team, is_admin(user))
    roles = ", ".join(dict.fromkeys(p.role for p in team))
    text = tr(
        "tk_home",
        roles=esc(roles),
        mine=len(mine),
        overdue=sum(1 for t in mine if t.due and t.due < today),
        risk=sum(1 for t in mine if t.status == RISK),
        review=len(review),
    )
    b = InlineKeyboardBuilder()
    b.button(text=tr("tk_btn_mine", n=len(mine)), callback_data=TasksCb(action="mine"))
    if review:
        b.button(text=tr("tk_btn_review", n=len(review)), callback_data=TasksCb(action="review"))
    if depts:
        b.button(text=tr("tk_btn_summary"), callback_data=TasksCb(action="summary"))
    b.button(text=tr("tk_btn_refresh"), callback_data=TasksCb(action="home", page=1))
    b.adjust(1)
    return text, b.as_markup()


@router.message(Command("my"), has_team)
@router.message(F.text.in_(all_variants("btn_tasks")))
async def cmd_tasks(message: Message, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, state: FSMContext, team: list[Person]) -> None:
    await state.clear()
    if not team or not board.enabled:
        await message.answer(tr("tk_not_team"))
        return
    text, kb = await home_view(board, session, team, user, tr, settings)
    await message.answer(text, reply_markup=kb)


@router.callback_query(TasksCb.filter(F.action == "home"))
async def cb_home(cb: CallbackQuery, callback_data: TasksCb, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, state: FSMContext, team: list[Person]) -> None:
    await state.clear()
    if not team:
        await cb.answer(tr("tk_not_team"), show_alert=True)
        return
    if callback_data.page:  # "refresh": read the sheet now
        await board.get(fresh=True)
    await cb.answer()
    text, kb = await home_view(board, session, team, user, tr, settings)
    await show(cb.message, text, kb, edit=True)


# --- Lists ---------------------------------------------------------------------------------

def list_kb(tasks: list[Task], page: int, action: str, tr: Translator, today: date) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in tasks[page * PAGE:(page + 1) * PAGE]:
        b.button(text=f"{task_icon(t, today)} {dm(t.due)} · {t.id} · {t.task}"[:64], callback_data=TaskCb(action="open", task_id=t.id, page=page))
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=TasksCb(action=action, page=page - 1).pack()))
    if (page + 1) * PAGE < len(tasks):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=TasksCb(action=action, page=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(back_home(tr))
    return b.as_markup()


@router.callback_query(TasksCb.filter(F.action == "mine"))
async def cb_mine(cb: CallbackQuery, callback_data: TasksCb, board: TaskBoard, tr: Translator, settings: Settings, team: list[Person]) -> None:
    if not team:
        await cb.answer(tr("tk_not_team"), show_alert=True)
        return
    snap = await board.get()
    today = today_in(settings.tz)
    tasks = board.my_tasks(snap, team, today)
    await cb.answer()
    text = tr("tk_mine_title", n=len(tasks)) if tasks else tr("tk_mine_empty")
    await show(cb.message, text, list_kb(tasks, callback_data.page, "mine", tr, today), edit=True)


@router.callback_query(TasksCb.filter(F.action == "review"))
async def cb_review(cb: CallbackQuery, callback_data: TasksCb, board: TaskBoard, session: AsyncSession, tr: Translator, settings: Settings, team: list[Person]) -> None:
    if not team:
        await cb.answer(tr("tk_not_team"), show_alert=True)
        return
    snap = await board.get()
    tasks = board.to_review(snap, team, await board.accepted_ids(session))
    await cb.answer()
    text = tr("tk_review_title", n=len(tasks)) if tasks else tr("tk_review_empty")
    await show(cb.message, text, list_kb(tasks, callback_data.page, "review", tr, today_in(settings.tz)), edit=True)


# --- Task card -----------------------------------------------------------------------------

async def card_view(snap: Snapshot, board: TaskBoard, session: AsyncSession, t: Task, team: list[Person], user: User, tr: Translator, settings: Settings, page: int) -> tuple[str, InlineKeyboardMarkup]:
    today = today_in(settings.tz)
    lines = [f'<b><a href="{esc(snap.book.link(t))}">{esc(t.id)}</a></b> · {STATUS_ICON.get(t.status, "")} {esc(t.status)}', esc(cut(t.task, 600)), ""]
    when = f"{dm(t.start)} → {dm(t.due)}" if t.start else dm(t.due)
    lines.append(tr("tk_card_when", when=when))
    if t.is_open and t.due and t.due < today:
        lines.append(tr("tk_card_overdue", days=(today - t.due).days))
    lines.append(tr("tk_card_r", r=esc(t.r or "—")))
    lines.append(tr("tk_card_a", a=esc(t.a or snap.book.default_a.get(t.dept, "") or "—")))
    if t.c:
        lines.append(tr("tk_card_c", c=esc(t.c)))
    if t.i:
        lines.append(tr("tk_card_i", i=esc(t.i)))
    if t.result:
        lines.append(tr("tk_card_result", result=esc(cut(t.result, 300))))
    if t.comment:
        lines.append(tr("tk_card_comment", comment=esc(cut(t.comment, 600))))
    meta = " · ".join(x for x in (t.dept, t.product, f"{t.hours:g} ч" if t.hours else "") if x)
    if meta:
        lines.append(f"📄 {esc(meta)}")
    st = await board.state(session, t.id)
    if t.status == DONE and st is not None and st.accepted_at:
        lines.append(tr("tk_card_accepted", by=esc(st.accepted_by or "—"), at=f"{st.accepted_at.astimezone(settings.tz):%d.%m}"))
    rights = board.rights(snap, t, team, is_admin(user))
    if st is not None and st.accepted_at:
        rights -= {"accept"}
    b = InlineKeyboardBuilder()
    for action in ("work", "done", "risk", "accept", "return", "cancel"):
        if action in rights:
            b.button(text=tr(f"tk_btn_{action}"), callback_data=TaskCb(action=action, task_id=t.id, page=page))
    b.adjust(2)
    b.row(InlineKeyboardButton(text=tr("tk_btn_back_list"), callback_data=TasksCb(action="mine", page=page).pack()))
    return "\n".join(lines), b.as_markup()


async def open_card(target: Message, task_id: str, *, board: TaskBoard, session: AsyncSession, team: list[Person], user: User, tr: Translator, settings: Settings, page: int, edit: bool) -> None:
    snap = await board.get()
    t = snap.book.by_id.get(task_id)
    if t is None:
        await target.answer(tr("tk_not_found"))
        return
    text, kb = await card_view(snap, board, session, t, team, user, tr, settings, page)
    await show(target, text, kb, edit=edit)


@router.callback_query(TaskCb.filter(F.action == "open"))
async def cb_open(cb: CallbackQuery, callback_data: TaskCb, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, team: list[Person]) -> None:
    if not team and not is_admin(user):
        await cb.answer(tr("tk_not_team"), show_alert=True)
        return
    await cb.answer()
    # Cards opened from a notification come as a new message; inside the lists they replace it.
    from_list = cb.message is not None and cb.message.reply_markup is not None and any(
        (btn.callback_data or "").startswith("tl:") for row in cb.message.reply_markup.inline_keyboard for btn in row
    )
    await open_card(cb.message, callback_data.task_id, board=board, session=session, team=team, user=user, tr=tr, settings=settings, page=callback_data.page, edit=from_list)


# --- Actions -------------------------------------------------------------------------------

def actor_name(cb_or_msg, team: list[Person]) -> str:
    names = [p.name or p.role for p in team]
    return names[0] if names else cb_or_msg.from_user.full_name


async def allowed(board: TaskBoard, task_id: str, action: str, team: list[Person], user: User) -> Task | None:
    snap = await board.get()
    t = snap.book.by_id.get(task_id)
    if t is None or action not in board.rights(snap, t, team, is_admin(user)):
        return None
    return t


async def apply_change(
    target: Message, *, task_id: str, action: str, reason: str, actor: str, board: TaskBoard, session: AsyncSession, team: list[Person], user: User, tr: Translator, settings: Settings, bot: Bot, page: int, edit: bool
) -> bool:
    change = await board.change(session, task_id=task_id, new=ACTION_STATUS[action], actor=team, actor_name=actor, reason=reason, today=today_in(settings.tz))
    if change is None:
        await target.answer(tr("tk_not_changed"))
        return False
    await open_card(target, task_id, board=board, session=session, team=team, user=user, tr=tr, settings=settings, page=page, edit=edit)
    await announce(bot, session, settings, board, change)
    return True


@router.callback_query(TaskCb.filter(F.action.in_({"work", "done"})))
async def cb_status(cb: CallbackQuery, callback_data: TaskCb, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, bot: Bot, team: list[Person]) -> None:
    if await allowed(board, callback_data.task_id, callback_data.action, team, user) is None:
        await cb.answer(tr("tk_no_rights"), show_alert=True)
        return
    await cb.answer(tr("tk_saving"))
    await apply_change(
        cb.message, task_id=callback_data.task_id, action=callback_data.action, reason="", actor=actor_name(cb, team),
        board=board, session=session, team=team, user=user, tr=tr, settings=settings, bot=bot, page=callback_data.page, edit=True,
    )


@router.callback_query(TaskCb.filter(F.action.in_({"risk", "return", "cancel"})))
async def cb_ask_reason(cb: CallbackQuery, callback_data: TaskCb, board: TaskBoard, user: User, tr: Translator, state: FSMContext, team: list[Person]) -> None:
    t = await allowed(board, callback_data.task_id, callback_data.action, team, user)
    if t is None:
        await cb.answer(tr("tk_no_rights"), show_alert=True)
        return
    await state.set_state(TaskSG.reason)
    await state.update_data(task_id=t.id, action=callback_data.action, page=callback_data.page)
    await cb.answer()
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_cancel"), callback_data=TaskCb(action="open", task_id=t.id, page=callback_data.page))
    await cb.message.answer(tr(f"tk_ask_{callback_data.action}", id=esc(t.id)), reply_markup=b.as_markup())


@router.message(TaskSG.reason, F.text)
async def on_reason(message: Message, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, state: FSMContext, bot: Bot, team: list[Person]) -> None:
    reason = " ".join(message.text.split())
    if not 3 <= len(reason) <= REASON_MAX:
        await message.answer(tr("tk_err_reason", max=REASON_MAX))
        return
    data = await state.get_data()
    await state.clear()
    if await allowed(board, data["task_id"], data["action"], team, user) is None:
        await message.answer(tr("tk_no_rights"))
        return
    await apply_change(
        message, task_id=data["task_id"], action=data["action"], reason=reason, actor=actor_name(message, team),
        board=board, session=session, team=team, user=user, tr=tr, settings=settings, bot=bot, page=data.get("page", 0), edit=False,
    )


@router.message(TaskSG.reason)
async def on_reason_not_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("tk_err_reason", max=REASON_MAX))


@router.callback_query(TaskCb.filter(F.action == "accept"))
async def cb_accept(cb: CallbackQuery, callback_data: TaskCb, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, bot: Bot, team: list[Person]) -> None:
    if await allowed(board, callback_data.task_id, "accept", team, user) is None:
        await cb.answer(tr("tk_no_rights"), show_alert=True)
        return
    review = await board.accept(session, task_id=callback_data.task_id, actor=team, actor_name=actor_name(cb, team), tg_id=cb.from_user.id)
    if review is None:
        await cb.answer(tr("tk_already_accepted"), show_alert=True)
        return
    await cb.answer(tr("tk_accepted"))
    await open_card(cb.message, callback_data.task_id, board=board, session=session, team=team, user=user, tr=tr, settings=settings, page=callback_data.page, edit=True)
    await announce(bot, session, settings, board, review)


# --- Summary for heads ---------------------------------------------------------------------

def dept_stats(tasks: list[Task], accepted: set[str], today: date) -> dict[str, int]:
    start, end = week_bounds(today)
    week = [t for t in tasks if t.due and start <= t.due <= end and t.status != CANCELLED]
    return {
        "week": len(week),
        "done": sum(1 for t in week if t.status == DONE),
        "overdue": sum(1 for t in tasks if t.is_open and t.due and t.due < today),
        "risk": sum(1 for t in tasks if t.status == RISK),
        "review": sum(1 for t in tasks if t.status == DONE and t.id not in accepted),
    }


@router.callback_query(TasksCb.filter(F.action == "summary"))
async def cb_summary(cb: CallbackQuery, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, team: list[Person]) -> None:
    snap = await board.get()
    depts = board.visible_depts(snap, team, is_admin(user))
    if not depts:
        await cb.answer(tr("tk_no_rights"), show_alert=True)
        return
    today = today_in(settings.tz)
    start, end = week_bounds(today)
    accepted = await board.accepted_ids(session)
    lines = [tr("tk_summary_title", start=dm(start), end=dm(end)), ""]
    load: dict[str, float] = defaultdict(float)
    b = InlineKeyboardBuilder()
    for n, d in enumerate(depts):
        tasks = [t for t in snap.tasks if t.dept == d]
        s = dept_stats(tasks, accepted, today)
        lines.append(tr("tk_summary_line", dept=esc(d), **s))
        b.button(text=d, callback_data=TasksCb(action="dept", page=n))
        for t in tasks:
            if t.is_open and t.hours and t.due and t.due <= end:
                for p in snap.r_of(t):
                    load[p.label] += t.hours
    if load:
        top = sorted(load.items(), key=lambda kv: -kv[1])[:6]
        lines += ["", tr("tk_summary_load", people="; ".join(f"{esc(k)} — {v:g}" for k, v in top))]
    b.adjust(2)
    b.row(back_home(tr))
    await cb.answer()
    await show(cb.message, "\n".join(lines), b.as_markup(), edit=True)


@router.callback_query(TasksCb.filter(F.action == "dept"))
async def cb_dept(cb: CallbackQuery, callback_data: TasksCb, board: TaskBoard, session: AsyncSession, user: User, tr: Translator, settings: Settings, team: list[Person]) -> None:
    snap = await board.get()
    depts = board.visible_depts(snap, team, is_admin(user))
    if not 0 <= callback_data.page < len(depts):
        await cb.answer(tr("tk_no_rights"), show_alert=True)
        return
    dept = depts[callback_data.page]
    today = today_in(settings.tz)
    accepted = await board.accepted_ids(session)
    tasks = sorted((t for t in snap.tasks if t.dept == dept), key=lambda t: (t.due or date.max, t.id))
    groups = [
        ("tk_dept_overdue", [t for t in tasks if t.is_open and t.due and t.due < today]),
        ("tk_dept_risk", [t for t in tasks if t.status == RISK]),
        ("tk_dept_review", [t for t in tasks if t.status == DONE and t.id not in accepted]),
    ]
    lines = [f"<b>{esc(dept)}</b>"]
    b = InlineKeyboardBuilder()
    shown = 0
    for key, items in groups:
        if not items:
            continue
        lines += ["", tr(key, n=len(items))]
        for t in items[:8]:
            extra = f" · 💬 {esc(cut(t.comment, 80))}" if key == "tk_dept_risk" and t.comment else ""
            lines.append(f"• {esc(t.id)} · {esc(cut(t.r, 40))} · {dm(t.due)}{extra}")
            if shown < 16:
                b.button(text=f"{task_icon(t, today)} {t.id} · {t.task}"[:64], callback_data=TaskCb(action="open", task_id=t.id))
                shown += 1
    if len(lines) == 1:
        lines.append(tr("tk_dept_clean"))
    b.adjust(1)
    b.row(InlineKeyboardButton(text=tr("btn_back"), callback_data=TasksCb(action="summary").pack()))
    await cb.answer()
    await show(cb.message, "\n".join(lines), b.as_markup(), edit=True)
