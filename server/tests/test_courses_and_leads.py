"""Proactive additions: the course catalog, leads and the sales chat, deep-link sources,
and the bridge that sends staff commands to the task spreadsheet."""

from datetime import UTC, date, datetime

import pytest
from aiogram.methods import EditMessageText
from aiogram.types import Chat, Contact, Message, Update
from sqlalchemy import select

from app.bot.handlers.common import split_payload
from app.db.models import Course, Lead, LeadStatus, User
from app.formatting import normalize_phone
from app.services import app_settings
from app.services.bridge import Bridge
from app.services.course_form import filled_form, parse_form
from tests.conftest import E2E_ADMIN as ADMIN
from tests.test_e2e_flow import Harness, h  # noqa: F401 - the fixture is used by name

CLIENT, SELLER, STRANGER, STAFFER = 501, 502, 503, 504
LEADS_CHAT = -100777

COURSE_FORM = """[Название RU]
Финансы для собственников
[Nomi UZ]
Biznes egalari uchun moliya
[Title EN]
-
[Описание RU]
Управленческий учёт на ваших цифрах
[Tavsif UZ]
-
[Description EN]
-
[Расписание RU]
пт и вс, 10:00–18:00
[Jadval UZ]
juma va yakshanba, 10:00–18:00
[Schedule EN]
-
[Старт]
20.11.2026
[Стоимость]
-"""


def texts_to(h: Harness, chat_id: int) -> list[str]:
    return [m.text for m in h.api.sent_to(chat_id)]


async def create_course(h: Harness, factory) -> Course:
    await h.send(ADMIN, "/start")
    await h.press(ADMIN, "lg:ru")
    await h.press(ADMIN, "ad:courses:0:0:")
    await h.press(ADMIN, "ad:new_course:0:0:")
    assert "[Расписание RU]" in h.api.sent_to(ADMIN)[-1].text  # the blank form
    await h.send(ADMIN, COURSE_FORM)
    await h.press(ADMIN, "ad:course_no_poster:0:0:")
    preview = h.api.sent_to(ADMIN)[-1].text
    assert "Стоимость — по запросу" in preview and "Название EN" in preview  # untranslated title is flagged
    await h.press(ADMIN, "ad:course_save:0:0:publish")
    async with factory() as s:
        course = (await s.execute(select(Course))).scalar_one()
    assert f"start=c_{course.id}" in h.api.sent_to(ADMIN)[-1].text  # the admin card with the share link
    return course


async def test_course_lead_journey(h: Harness, factory):
    course = await create_course(h, factory)
    assert course.status == "published" and course.price_tiyin is None and course.starts_on == date(2026, 11, 20)
    assert course.loc("title", "uz") == "Biznes egalari uchun moliya" and course.loc("title", "en") == "Финансы для собственников"
    async with factory() as s:
        await app_settings.set_leads_chat(s, LEADS_CHAT, "Продажи")
        await s.commit()

    # --- a client comes from an Instagram link straight to the course, in Uzbek ---------------
    await h.send(CLIENT, f"/start c_{course.id}-ig")
    await h.press(CLIENT, "lg:uz")
    card = h.api.sent_to(CLIENT)[-1]
    assert "Biznes egalari uchun moliya" in card.text and "20-noyabr, 2026" in card.text and "juma va yakshanba" in card.text
    await h.press(CLIENT, h.button(CLIENT, "ld:start"))
    await h.send(CLIENT, "Азиз Каримов")
    await h.send(CLIENT, "90 123 45 67")  # typed instead of the contact button
    await h.send(CLIENT, "⏭ Oʻtkazib yuborish")
    assert "+998901234567" in h.api.sent_to(CLIENT)[-1].text  # the summary
    await h.press(CLIENT, h.button(CLIENT, "ld:send"))
    assert "№1 ariza qabul qilindi" in h.api.sent_to(CLIENT)[-1].text

    async with factory() as s:
        lead = (await s.execute(select(Lead))).scalar_one()
        client = (await s.execute(select(User).where(User.tg_id == CLIENT))).scalar_one()
    assert (lead.name, lead.phone, lead.comment, lead.lang, lead.source) == ("Азиз Каримов", "+998901234567", None, "uz", "ig")
    assert client.source == "ig" and client.phone == "+998901234567"

    # The card in the sales chat is in the chat's language, with status buttons.
    posted = h.api.sent_to(LEADS_CHAT)[-1]
    assert "Заявка №1" in posted.text and "Финансы для собственников" in posted.text and "@user501" in posted.text
    assert lead.card_chat_id == LEADS_CHAT

    # The same client cannot pile up a second open lead for this course.
    await h.press(CLIENT, f"ld:start:{course.id}")
    assert "allaqachon №1" in h.api.sent_to(CLIENT)[-1].text

    # --- sales: anyone in the leads chat takes it; a second "take" is refused ---------------
    await h.press(SELLER, f"sl:take:{lead.id}", chat_id=LEADS_CHAT)
    await h.press(STRANGER, f"sl:take:{lead.id}", chat_id=LEADS_CHAT)
    assert any("уже в работе у Name502" in a for a in h.answers(STRANGER))
    edits = [c for c in h.api.calls if isinstance(c, EditMessageText)]
    assert edits and "В работе у Name502" in edits[-1].text

    # Outside the chat, a random user has no rights.
    await h.press(STRANGER, f"sl:rejected:{lead.id}")
    assert "Недостаточно прав." in h.answers(STRANGER)

    await h.press(SELLER, f"sl:enrolled:{lead.id}", chat_id=LEADS_CHAT)
    async with factory() as s:
        lead = await s.get(Lead, lead.id)
    assert lead.status == LeadStatus.ENROLLED and lead.manager_name.startswith("Name502")

    # The admin's course card counts it.
    await h.press(ADMIN, f"ad:course:0:{course.id}:")
    card = [c.text for c in h.api.calls if isinstance(c, EditMessageText) and "📋 Заявки:" in c.text][-1]
    assert "🙋 0 · ✅ 1 · ❌ 0" in card


async def test_consultation_without_leads_chat_goes_to_admins(h: Harness, factory):
    await h.send(ADMIN, "/start")
    await h.press(ADMIN, "lg:ru")
    await h.send(CLIENT, "/start")
    await h.press(CLIENT, "lg:en")
    await h.send(CLIENT, "📝 Leave a request")
    await h.press(CLIENT, h.button(CLIENT, "ld:start"))  # no courses: only "consultation"
    await h.send(CLIENT, "Aziz")
    await h.send(CLIENT, contact=Contact(phone_number="998901112233", first_name="Aziz", user_id=CLIENT))
    await h.send(CLIENT, "Call me after 6pm, please <b>")
    summary = h.api.sent_to(CLIENT)[-1].text
    assert "Consultation" in summary and "&lt;b&gt;" in summary
    await h.press(CLIENT, h.button(CLIENT, "ld:send"))

    card = h.api.sent_to(ADMIN)[-1]
    assert "Заявка №1" in card.text and "Консультация" in card.text and "Call me after 6pm" in card.text
    # The admin works it right from the DM.
    await h.press(ADMIN, h.button(ADMIN, "sl:take"))
    async with factory() as s:
        assert (await s.execute(select(Lead))).scalar_one().status == LeadStatus.IN_WORK


async def test_admin_leads_list(h: Harness, factory):
    await test_consultation_without_leads_chat_goes_to_admins(h, factory)
    await h.press(ADMIN, "ad:leads:0:0:")
    assert any(isinstance(c, EditMessageText) and "Открытых: 1" in c.text for c in h.api.calls)


# --- Bridge to the task spreadsheet --------------------------------------------------------

class FakeBridge(Bridge):
    def __init__(self, staff_usernames: set[str], down: bool = False):
        super().__init__("https://script.google.com/macros/s/x/exec", "secret")
        self.staff_usernames, self.down = staff_usernames, down
        self.forwarded: list[str] = []
        self.registered: set[int] = set()

    async def _post(self, payload: dict) -> dict:
        if self.down:
            raise RuntimeError("timeout")
        if payload["action"] == "staff":
            return {"ok": True, "usernames": sorted(self.staff_usernames), "chat_ids": [str(c) for c in self.registered]}
        msg = payload["message"]
        self.forwarded.append(msg["text"])
        known = (msg["from"].get("username") or "").lower() in self.staff_usernames
        if known and msg["text"].startswith("/start"):
            self.registered.add(msg["chat"]["id"])
        return {"ok": True, "client": not known}


@pytest.fixture
def bridge(dispatcher):
    original = dispatcher["bridge"]
    yield lambda b: dispatcher.__setitem__("bridge", b)
    dispatcher["bridge"] = original


async def test_bridge_routes_staff_commands(h: Harness, factory, bridge):
    fake = FakeBridge({f"user{STAFFER}"})
    bridge(fake)

    # Staff: /start registers them in the spreadsheet once, then the usual menu.
    await h.send(STAFFER, "/start")
    await h.press(STAFFER, "lg:ru")
    await h.send(STAFFER, "/start")
    assert fake.forwarded == ["/start"]
    assert any("Здравствуйте" in t for t in texts_to(h, STAFFER))

    # Staff /my: their tasks come from the spreadsheet, the bot stays silent.
    before = len(h.api.sent_to(STAFFER))
    await h.send(STAFFER, "/my")
    assert fake.forwarded[-1] == "/my" and len(h.api.sent_to(STAFFER)) == before

    # A client's /my is "My tickets"; nothing goes to the spreadsheet.
    await h.send(CLIENT, "/start")
    await h.press(CLIENT, "lg:ru")
    await h.send(CLIENT, "/my")
    assert fake.forwarded[-1] == "/my" and len(fake.forwarded) == 2
    assert "Пока пусто" in h.api.sent_to(CLIENT)[-1].text

    # /test is always asked; a stranger falls through to the bot.
    await h.send(CLIENT, "/test 123456")
    assert fake.forwarded[-1] == "/test 123456"
    assert "Не совсем понял" in h.api.sent_to(CLIENT)[-1].text

    # A group subscribes to a role.
    group = Chat(id=-100555, type="group", title="Продавцы")
    msg = Message(message_id=900, date=datetime.now(UTC), chat=group, from_user=h.user(STAFFER), text="/start Продавцы")
    await h.dp.feed_update(h.bot, Update(update_id=10_000, message=msg))
    assert fake.forwarded[-1] == "/start Продавцы" and not h.api.sent_to(group.id)

    # The spreadsheet does not answer: staff are told so.
    fake.down = True
    await h.send(STAFFER, "/my")
    assert "Служебные команды сейчас недоступны" in h.api.sent_to(STAFFER)[-1].text


async def test_bridge_off_keeps_bot_commands(h: Harness, factory, bridge):
    bridge(Bridge(None, None))
    await h.send(STAFFER, "/start")
    await h.press(STAFFER, "lg:ru")
    await h.send(STAFFER, "/my")
    assert "Пока пусто" in h.api.sent_to(STAFFER)[-1].text


# --- Small pieces --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, (None, None)),
        ("ev_12", ("ev_12", None)),
        ("c_3-ig", ("c_3", "ig")),
        ("ig_reels", (None, "ig_reels")),
        ("bad payload!", (None, None)),
    ],
)
def test_split_payload(payload, expected):
    assert split_payload(payload) == expected


def test_checkin_payload_is_not_a_source():
    from app.services.tickets import CHECKIN_PREFIX

    target, source = split_payload(f"{CHECKIN_PREFIX}abc-DEF_1")
    assert target.startswith(CHECKIN_PREFIX) and source is None


@pytest.mark.parametrize(
    ("text", "phone"),
    [("90 123 45 67", "+998901234567"), ("+998 (90) 123-45-67", "+998901234567"), ("12345", None), ("+7 912 345 67 89", "+79123456789")],
)
def test_normalize_phone(text, phone):
    assert normalize_phone(text) == phone


def test_course_form_round_trip():
    form, errors = parse_form(COURSE_FORM.replace("[Стоимость]\n-", "[Стоимость]\n3 000 000"), ui_lang="ru")
    assert errors == []
    course = Course(title="x")
    form.apply_to(course)
    assert course.price_tiyin == 300_000_000 and course.schedule == "пт и вс, 10:00–18:00"
    again, errors = parse_form(filled_form(course, "uz"), ui_lang="ru")  # labels in another language still parse
    assert errors == [] and again.texts == form.texts and again.price_tiyin == form.price_tiyin


def test_course_form_errors():
    form, errors = parse_form("[Название RU]\n-\n[Старт]\n31.02.2026\n[Стоимость]\nдорого", ui_lang="ru")
    assert form is None and len(errors) == 3
