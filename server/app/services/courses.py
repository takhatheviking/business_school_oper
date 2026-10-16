"""Course catalog and leads: requests to join a course or to get a consultation."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OPEN_LEAD_STATUSES, Course, CourseStatus, Lead, LeadStatus, User
from app.services.booking import now_utc

# A status change a manager can make from the lead card: action -> new status.
LEAD_ACTIONS = {
    "take": LeadStatus.IN_WORK,
    "enrolled": LeadStatus.ENROLLED,
    "rejected": LeadStatus.REJECTED,
    "reopen": LeadStatus.IN_WORK,
}


def _catalog_order():
    return Course.starts_on.asc().nulls_last(), Course.id


async def list_published(session: AsyncSession) -> list[Course]:
    rows = await session.execute(select(Course).where(Course.status == CourseStatus.PUBLISHED).order_by(*_catalog_order()))
    return list(rows.scalars())


async def list_for_admin(session: AsyncSession) -> list[Course]:
    rows = await session.execute(select(Course).order_by(Course.status.desc(), *_catalog_order()))
    return list(rows.scalars())


async def lead_counts(session: AsyncSession, course_id: int | None = None) -> dict[str, int]:
    """Leads per status, for one course or for all of them."""
    stmt = select(Lead.status, func.count()).group_by(Lead.status)
    if course_id is not None:
        stmt = stmt.where(Lead.course_id == course_id)
    counts = {s.value: 0 for s in LeadStatus}
    counts.update({status: n for status, n in (await session.execute(stmt)).all()})
    return counts


async def open_lead(session: AsyncSession, user_id: int, course_id: int | None) -> Lead | None:
    stmt = select(Lead).where(Lead.user_id == user_id, Lead.status.in_(OPEN_LEAD_STATUSES))
    stmt = stmt.where(Lead.course_id.is_(None) if course_id is None else Lead.course_id == course_id)
    return (await session.execute(stmt.order_by(Lead.id.desc()).limit(1))).scalars().first()


async def last_lead_name(session: AsyncSession, user_id: int) -> str | None:
    """The name from the person's previous lead, offered as a one-tap answer next time."""
    stmt = select(Lead.name).where(Lead.user_id == user_id).order_by(Lead.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_lead(
    session: AsyncSession, *, user: User, course_id: int | None, name: str, phone: str, comment: str | None, lang: str | None
) -> tuple[Lead, bool]:
    """Returns (lead, created). An open lead for the same course is returned instead of a new one. Commits."""
    existing = await open_lead(session, user.id, course_id)
    if existing is not None:
        return existing, False
    lead = Lead(user_id=user.id, course_id=course_id, name=name, phone=phone, comment=comment, lang=lang, source=user.source)
    session.add(lead)
    try:
        await session.commit()
    except IntegrityError:
        # A parallel tap won the unique index: show that lead.
        await session.rollback()
        existing = await open_lead(session, user.id, course_id)
        if existing is None:
            raise
        return existing, False
    await session.refresh(lead, ["user", "course"])
    return lead, True


@dataclass
class LeadChange:
    lead: Lead
    changed: bool


async def change_lead_status(session: AsyncSession, *, lead_id: int, action: str, manager_tg_id: int, manager_name: str) -> LeadChange | None:
    """Applies a manager's button press. Row lock: two managers pressing "take" at once
    end up with one owner. Commits."""
    status = LEAD_ACTIONS.get(action)
    lead = (await session.execute(select(Lead).where(Lead.id == lead_id).with_for_update(of=Lead))).scalars().first()
    if lead is None or status is None:
        await session.rollback()
        return None
    if (action == "take" and lead.status != LeadStatus.NEW) or (action == "reopen" and lead.status in OPEN_LEAD_STATUSES):
        await session.rollback()  # releases the lock and expires the row: load it again for the caller
        await session.refresh(lead)  # columns and the eagerly loaded user and course
        return LeadChange(lead, False)
    lead.status = status
    lead.status_changed_at = now_utc()
    if action in ("take", "reopen") or lead.manager_tg_id is None:
        lead.manager_tg_id, lead.manager_name = manager_tg_id, manager_name
    await session.commit()
    await session.refresh(lead, ["user", "course"])
    return LeadChange(lead, True)


async def open_leads(session: AsyncSession, limit: int = 15) -> tuple[list[Lead], int]:
    base = select(Lead).where(Lead.status.in_(OPEN_LEAD_STATUSES))
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = await session.execute(base.order_by(Lead.status, Lead.id.desc()).limit(limit))
    return list(rows.scalars()), total


async def all_leads(session: AsyncSession) -> list[Lead]:
    return list((await session.execute(select(Lead).order_by(Lead.id.desc()))).scalars())
