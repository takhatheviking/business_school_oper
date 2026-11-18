"""Autoposting the content plan: reading the plan sheet, files from the content team, the
schedule (publishing, reminders, misses, posts made by hand), "Publish now" without doubles,
and connecting the channel and the sheet from the bot."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetChat, GetChatMember, SendDocument, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import (
    Chat,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberUpdated,
    Document,
    PhotoSize,
    ReplyKeyboardMarkup,
    Update,
)
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.bot.callbacks import ChannelCb
from app.config import Settings
from app.db.models import ChannelPost, UserRole
from app.services import app_settings
from app.services.app_settings import ChannelInfo
from app.services.autopost import attach, planner, publish, split_text, tg_len, tick
from app.services.autopost import state as post_state
from app.services.content_plan import DOCUMENT, MANUAL, PHOTO, SERIAL_EPOCH, TEXT, find_header, format_kind, parse_posts, to_time
from tests.conftest import E2E_ADMIN as ADMIN
from tests.conftest import make_user
from tests.test_e2e_flow import FakeSession, Harness

CHANNEL = -1001234567890
EDITOR, CLIENT = 701, 702
SETTINGS = Settings(bot_token="123456:TEST", admin_ids=str(ADMIN), _env_file=None)
TZ = SETTINGS.tz
HEAD = [
    "№", "Sana", "Vaqt", "Bosqich", "Rubrika", "Format", "Maqsad", "Sarlavha / Hook", "TELEGRAMGA TAYYOR POST MATNI",
    "SPIKER UCHUN TO‘LIQ SSENARIY", "BANNER / PDF ICHIDAGI TAYYOR MATN", "ISHLAB CHIQARISH TOPSHIRIG‘I", "Mas’ul", "Status",
]
STATUS = HEAD.index("Status")


def slot(delta: timedelta) -> tuple[int, str]:
    """(date serial, "HH:MM") of now + delta in Tashkent, as the sheet keeps them."""
    local = datetime.now(TZ) + delta
    return (local.date() - SERIAL_EPOCH).days, f"{local:%H:%M}"


def row(no: str, delta: timedelta, fmt: str, title: str, text: str, status: str = "Rejada") -> list:
    d, t = slot(delta)
    return [no, d, t, "1. Qiziqish va diagnostika", "Rubrika", fmt, "Maqsad", title, text, "", "", "", "AI dizayn", status]


class FakePlan:
    url = "https://docs.google.com/spreadsheets/d/PLAN/edit"

    def __init__(self, rows: list[list], sheet_id: str = "PLAN") -> None:
        self.sheet_id = sheet_id
        self.values = [["MARKETING MAXIMUM — TELEGRAM LAUNCH CONTENT PLAN"], ["Har bir qator post qo‘yishga tayyor"], [], HEAD, *rows]
        self.writes: list[tuple[str, str]] = []

    async def read(self):
        return "Kontent reja", 312774367, copy.deepcopy(self.values)

    async def write_status(self, gid: int, no: str, value: str) -> bool:
        self.writes.append((no, value))
        for r in self.values[4:]:
            if str(r[0]) == no:
                r[STATUS] = value
                return True
        return False

    def status(self, no: str) -> str:
        return next(r[STATUS] for r in self.values[4:] if str(r[0]) == no)


class ChannelApi(FakeSession):
    """The fake Telegram API, plus the channel: its info and the bot's rights there."""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        if isinstance(method, GetChat):
            self.calls.append(method)
            return ChatFullInfo.model_construct(id=method.chat_id, type="channel", title="Зал ожиданий", username=None)
        if isinstance(method, GetChatMember) and method.user_id == bot.id:
            self.calls.append(method)
            return ChatMemberAdministrator.model_construct(status="administrator", can_post_messages=True)
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def ch(factory, dispatcher):
    dispatcher.fsm.storage = MemoryStorage()
    dispatcher["throttle"].limit = 10_000
    api = ChannelApi()
    bot = Bot("123456:TEST", session=api, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    async with factory() as s:
        await make_user(s, ADMIN, role=UserRole.ADMIN)
        await make_user(s, EDITOR, role=UserRole.CONTENT)
        await make_user(s, CLIENT)
    yield Harness(dispatcher, bot, api)
    planner.reset()


async def connect(factory, fake: FakePlan, *, channel: bool = True) -> None:
    planner.reset()
    planner.make_backend = lambda sheet_id, settings: fake
    async with factory() as s:
        await app_settings.set_content_sheet(s, fake.sheet_id)
        if channel:
            await app_settings.set_content_channel(s, ChannelInfo(CHANNEL, "Зал ожиданий", None))
        await s.commit()


def to_channel(h: Harness, kind: type = SendMessage) -> list:
    return h.api.sent_to(CHANNEL, kind)


def texts(h: Harness, chat_id: int) -> list[str]:
    return [m.text for m in h.api.sent_to(chat_id)]


# --- reading the plan -----------------------------------------------------------------------

def test_plan_is_read_like_the_real_sheet():
    d, _ = slot(timedelta(0))
    values = [
        ["MARKETING MAXIMUM 15-GURUH — TELEGRAM LAUNCH CONTENT PLAN (FINAL)"],
        ["HAR BIR QATOR POST QO‘YISHGA TAYYOR"],
        [],
        HEAD,
        ["1", d, "09:30", "1.", "", "Yozuv", "", "BU GURUH FAQAT KURS SOTISH UCHUN OCHILMADI", "Assalomu alaykum.\n\nMatn", "", "", "", "Kontent menejer", "Rejada"],
        ["2", d, "12:30", "1.", "", "Dumaloq video", "", "MARKETING TARGETDAN BOSHLANMAYDI", "🎥 Spikerdan", "", "", "", "Spiker", "Rejada"],
        ["3", d, 0.6875, "1.", "", "Dizayn banner", "", "", "Banner matni", "", "", "", "AI dizayn", "Tayyor"],
        ["3", d, "18:00", "1.", "", "PDF", "", "dublikat", "", "", "", "", "", ""],  # a duplicated number: the first row wins
        ["4", "", "20:30", "1.", "", "PDF", "", "Sanasiz", "PDF matni", "", "", "", "", "Bekor qilindi"],
        [],
        ["5", f"{(SERIAL_EPOCH + timedelta(days=d)):%d.%m.%Y}", "8:05", "1.", "", "", "", "", "Matn", "", "", "", "", "Joylandi"],
    ]
    assert find_header(values)[0] == 3
    posts = {p.no: p for p in parse_posts(values, TZ)}
    assert list(posts) == ["1", "2", "3", "4", "5"]
    one = posts["1"]
    assert (one.kind, one.title, one.text, one.owner, one.row) == (TEXT, "BU GURUH FAQAT KURS SOTISH UCHUN OCHILMADI", "Assalomu alaykum.\n\nMatn", "Kontent menejer", 5)
    assert one.due.tzinfo is TZ and f"{one.due:%H:%M}" == "09:30" and (one.due.date() - SERIAL_EPOCH).days == d
    assert posts["2"].kind == MANUAL
    assert posts["3"].kind == PHOTO and f"{posts['3'].due:%H:%M}" == "16:30" and posts["3"].label == "Banner matni"
    assert posts["4"].kind == DOCUMENT and posts["4"].due is None and posts["4"].cancelled
    assert posts["5"].kind == TEXT and posts["5"].posted and f"{posts['5'].due:%H:%M}" == "08:05"
    assert [format_kind(f) for f in ("Yozuv", "Dizayn banner", "PDF", "Dumaloq video", "Reels")] == [TEXT, PHOTO, DOCUMENT, MANUAL, MANUAL]
    assert to_time("24:00") is None and to_time(0.5).hour == 12


def test_long_texts_are_split_by_telegram_length():
    para = "Marketing tizimi 📈 " * 60  # emoji count twice in Telegram's limits
    text = "\n\n".join([para] * 6)
    pieces = split_text(text)
    assert len(pieces) > 1 and all(tg_len(p) <= 4096 for p in pieces)
    assert "".join(pieces).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")
    assert split_text("Qisqa") == ["Qisqa"]


# --- files from the content team -------------------------------------------------------------

async def test_editor_attaches_a_banner_by_number(ch: Harness, factory):
    fake = FakePlan([
        row("10", timedelta(days=1), "Dizayn banner", "MARKETINGNING 5 QATLAMI", "Bugungi sxemani saqlab qo‘ying <b>."),
        row("14", timedelta(days=1), "PDF", "MIJOZNI TANLASH", "📎 PDF matni"),
    ])
    await connect(factory, fake)

    await ch.send(EDITOR, "/start")
    await ch.press(EDITOR, "lg:ru")
    menu = [m.reply_markup for m in ch.api.sent_to(EDITOR) if isinstance(m.reply_markup, ReplyKeyboardMarkup)][-1]
    assert menu.keyboard[-1][0].text == "📣 Канал"
    await ch.send(EDITOR, "📣 Канал")
    home = ch.api.sent_to(EDITOR)[-1].text
    assert "Автопостинг в канал" in home and "Зал ожиданий" in home and "постов: 2" in home and "❗ Нет файла: №10" in home

    await ch.send(EDITOR, photo=[PhotoSize(file_id="banner-10", file_unique_id="b10", width=1080, height=1350)], caption="№10")
    preview = ch.api.sent_to(EDITOR, SendPhoto)[-1]
    assert preview.photo == "banner-10" and preview.caption == "Bugungi sxemani saqlab qo‘ying <b>." and preview.parse_mode is None
    assert "Файл прикреплён к №10" in ch.api.sent_to(EDITOR)[-1].text
    assert fake.status("10") == "Tayyor"
    async with factory() as s:
        st = (await s.execute(select(ChannelPost).where(ChannelPost.post_no == "10"))).scalar_one()
        assert (st.file_id, st.file_kind, st.file_by) == ("banner-10", "photo", f"Name{EDITOR} Test")

    # A PDF for the banner post does not fit.
    pdf = Document(file_id="pdf-1", file_unique_id="p1", file_name="audit.pdf", mime_type="application/pdf")
    await ch.send(EDITOR, document=pdf, caption="10")
    assert "к нему нужен PDF" not in ch.api.sent_to(EDITOR)[-1].text and "к нему нужен баннер" in ch.api.sent_to(EDITOR)[-1].text


async def test_file_without_number_offers_the_posts_that_need_it(ch: Harness, factory):
    fake = FakePlan([
        row("10", timedelta(days=1), "Dizayn banner", "BANNER", "Banner matni"),
        row("14", timedelta(days=1), "PDF", "MIJOZNI TANLASH", "📎 Bugungi amaliy material"),
        row("22", timedelta(days=2), "PDF", "ALTERNATIVLAR", "📎 PDF 2"),
        row("6", timedelta(hours=-5), "PDF", "ESKI", "📎 eski", status="Joylandi"),
    ])
    await connect(factory, fake)
    pdf = Document(file_id="pdf-14", file_unique_id="p14", file_name="mini-karta.pdf", mime_type="application/pdf")
    await ch.send(EDITOR, document=pdf)
    offer = ch.api.sent_to(EDITOR)[-1]
    assert "Для какого поста этот PDF?" in offer.text
    data = [b.callback_data for r in offer.reply_markup.inline_keyboard for b in r]
    assert data == [ChannelCb(action="attach", no="14").pack(), ChannelCb(action="attach", no="22").pack()]  # no banners, nothing published

    await ch.press(EDITOR, data[0])
    sent = ch.api.sent_to(EDITOR, SendDocument)[-1]
    assert sent.document == "pdf-14" and sent.caption == "📎 Bugungi amaliy material"
    assert "Файл прикреплён к №14" in ch.api.sent_to(EDITOR)[-1].text
    assert fake.writes == [("14", "Tayyor")]

    # Clients' photos are not content: they go to the receipt flow as before.
    await ch.send(CLIENT, photo=[PhotoSize(file_id="receipt", file_unique_id="r", width=10, height=10)], caption="14")
    async with factory() as s:
        assert len((await s.execute(select(ChannelPost))).scalars().all()) == 1


# --- the schedule ----------------------------------------------------------------------------

async def test_schedule_publishes_reminds_and_reports_once(ch: Harness, factory):
    long_text = "Uzun matn. " * 150  # longer than a caption may be
    fake = FakePlan([
        row("1", timedelta(minutes=-1), "Yozuv", "BU GURUH OCHILDI", "Assalomu alaykum.\n\n<i>Matn</i>"),
        row("2", timedelta(hours=2), "Dizayn banner", "5 QATLAM", "Banner"),
        row("3", timedelta(minutes=30), "PDF", "MINI-AUDIT", "PDF"),
        row("4", timedelta(minutes=-2), "Dizayn banner", "FAYLSIZ", "Banner"),
        row("5", timedelta(minutes=-60), "Yozuv", "KECHIKDI", "Kech"),
        row("6", timedelta(minutes=10), "Dumaloq video", "MARKETINGDA 3 DARAJA", "🎥 Spikerdan mini-dars"),
        row("7", timedelta(minutes=-1), "Yozuv", "BEKOR", "Bekor", status="Bekor qilindi"),
        row("8", timedelta(minutes=-1), "Yozuv", "QO‘LDA", "Qo‘lda joylandi", status="Joylandi"),
        row("9", timedelta(minutes=-1), "Dizayn banner", "UZUN", long_text),
        row("11", timedelta(days=-2), "Yozuv", "ESKI", "Eski"),  # long gone: history, not news
    ])
    await connect(factory, fake)
    async with factory() as s:
        plan = await planner.refresh(s, SETTINGS, force=True)
        await attach(s, plan, plan.by_no["9"], file_id="banner-9", file_kind="photo", name=None, by="Takhir")
        await tick(ch.bot, s, SETTINGS)

    texts_out = to_channel(ch)
    assert texts_out[0].text == "Assalomu alaykum.\n\n<i>Matn</i>" and texts_out[0].parse_mode is None
    photo = to_channel(ch, SendPhoto)
    assert len(photo) == 1 and photo[0].photo == "banner-9" and photo[0].caption is None  # too long for a caption...
    assert texts_out[1].text == long_text.strip()  # ...so the text follows
    assert len(texts_out) == 2  # nothing else went to the channel
    assert ("1", "Joylandi") in fake.writes and ("9", "Joylandi") in fake.writes and fake.status("7") == "Bekor qilindi"

    for who in (EDITOR, ADMIN):
        got = "\n".join(texts(ch, who))
        assert "Опубликован №1" in got and "Опубликован №9" in got
        assert "Пост №2 «5 QATLAM» выходит в" in got and "Пост №3 «MINI-AUDIT» выходит в" in got
        assert "№4 «FAYLSIZ»" in got and "нет файла (баннер)" in got
        assert "Не вышли вовремя" in got and "№5" in got and "№11" not in got
        assert "по плану №6 «MARKETINGDA 3 DARAJA» (Dumaloq video)" in got
        assert "🎥 Spikerdan mini-dars" in texts(ch, who)  # the text to post with the video, ready to copy
    assert "Клиент" not in "".join(texts(ch, CLIENT))  # clients hear nothing

    before = len(ch.api.calls)
    async with factory() as s:
        await tick(ch.bot, s, SETTINGS)
        planner.last_poll = float("-inf")  # a fresh read too
        await tick(ch.bot, s, SETTINGS)
    assert len(ch.api.calls) == before  # every message once

    async with factory() as s:
        sts = {st.post_no: st for st in (await s.execute(select(ChannelPost))).scalars()}
    assert sts["1"].posted_at and sts["1"].channel_id == CHANNEL and sts["1"].sheet_marked and sts["1"].posted_by is None
    assert set(sts["2"].notified) == {"3h"} and set(sts["3"].notified) == {"1h"} and set(sts["4"].notified) == {"due"}
    assert set(sts["5"].notified) == {"late"} and set(sts["6"].notified) == {"manual"}


async def test_no_channel_means_no_posts_and_one_alert(ch: Harness, factory):
    fake = FakePlan([row("1", timedelta(minutes=-1), "Yozuv", "MATN", "Matn")])
    await connect(factory, fake, channel=False)
    async with factory() as s:
        await tick(ch.bot, s, SETTINGS)
        await tick(ch.bot, s, SETTINGS)
    assert to_channel(ch) == []
    assert sum("канал не подключён" in t for t in texts(ch, ADMIN)) == 1


async def test_publish_now_goes_out_once(ch: Harness, factory):
    fake = FakePlan([
        row("20", timedelta(days=1), "Yozuv", "FAQ", "Eng ko‘p beriladigan savollar"),
        row("21", timedelta(minutes=-1), "Yozuv", "TO‘XTATILDI", "Bu chiqmasligi kerak"),
    ])
    await connect(factory, fake)
    await ch.press(EDITOR, ChannelCb(action="post", no="20").pack())
    await ch.press(EDITOR, ChannelCb(action="pub", no="20").pack())
    await ch.press(EDITOR, ChannelCb(action="pub_go", no="20").pack())
    await ch.press(EDITOR, ChannelCb(action="pub_go", no="20").pack())
    assert [m.text for m in to_channel(ch)] == ["Eng ko‘p beriladigan savollar"]
    assert "№20 опубликован" in ch.answers(EDITOR)[-2] and "уже опубликован" in ch.answers(EDITOR)[-1]

    # "Do not publish": the schedule passes the post by.
    await ch.press(EDITOR, ChannelCb(action="skip", no="21").pack())
    async with factory() as s:
        await tick(ch.bot, s, SETTINGS, now=datetime.now(UTC) + timedelta(days=1, minutes=1))
        plan = planner.plan
        st = (await s.execute(select(ChannelPost).where(ChannelPost.post_no == "20"))).scalar_one()
        assert st.posted_by == f"Name{EDITOR} Test"
    assert len(to_channel(ch)) == 1

    # Two presses at the same moment: one post.
    fake.values.append(row("22", timedelta(days=1), "Yozuv", "IKKI", "Bir marta"))
    async with factory() as s:
        plan = await planner.refresh(s, SETTINGS, force=True)
    async with factory() as s1, factory() as s2:
        results = await asyncio.gather(
            publish(ch.bot, s1, SETTINGS, plan, plan.by_no["22"], by="a"), publish(ch.bot, s2, SETTINGS, plan, plan.by_no["22"], by="b")
        )
    assert sorted(r.ok for r in results) == [False, True]
    assert [m.text for m in to_channel(ch)].count("Bir marta") == 1


# --- settings from the bot -------------------------------------------------------------------

async def test_super_admin_connects_the_channel_and_the_sheet(ch: Harness, factory):
    planner.reset()
    sheet_id = "1qsnTESTSHEETidForTheContentPlan_0001"
    fake = FakePlan([row("1", timedelta(days=1), "Yozuv", "MATN", "Matn")], sheet_id=sheet_id)
    planner.make_backend = lambda sid, settings: fake if sid == sheet_id else None

    bot_user = TgUser(id=ch.bot.id, is_bot=True, first_name="Proactive")
    added = ChatMemberUpdated.model_construct(
        chat=Chat(id=CHANNEL, type="channel", title="Зал ожиданий"),
        from_user=ch.user(ADMIN),
        date=datetime.now(UTC),
        old_chat_member=ChatMemberLeft(user=bot_user),
        new_chat_member=ChatMemberAdministrator.model_construct(status="administrator", user=bot_user, can_post_messages=True),
    )
    await ch.dp.feed_update(ch.bot, Update.model_construct(update_id=900, my_chat_member=added).as_(ch.bot))
    use = ChannelCb(action="use", no=str(CHANNEL)).pack()
    assert use in [b.callback_data for r in ch.api.last_markup(ADMIN).inline_keyboard for b in r]

    await ch.press(EDITOR, use)  # a content manager may not choose where the school publishes
    async with factory() as s:
        assert await app_settings.get_content_channel(s, SETTINGS) is None
    await ch.press(ADMIN, use)
    async with factory() as s:
        channel = await app_settings.get_content_channel(s, SETTINGS)
    assert (channel.chat_id, channel.title) == (CHANNEL, "Зал ожиданий")

    await ch.press(ADMIN, ChannelCb(action="sheet").pack())
    await ch.send(ADMIN, "https://docs.google.com/spreadsheets/d/nope")
    assert "Не похоже на ссылку" in texts(ch, ADMIN)[-1]
    await ch.send(ADMIN, f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit?usp=sharing")
    assert any("Контент-план подключён: лист «Kontent reja», постов: 1" in t for t in texts(ch, ADMIN))
    async with factory() as s:
        assert await app_settings.get_content_sheet(s, SETTINGS) == sheet_id
    assert planner.plan is not None and planner.plan.sheet_id == sheet_id


async def test_a_send_cut_short_by_a_restart_is_not_repeated(ch: Harness, factory):
    fake = FakePlan([row("30", timedelta(minutes=-1), "Yozuv", "QAROR", "Ikki xil to‘g‘ri qaror bor")])
    await connect(factory, fake)
    async with factory() as s:
        plan = await planner.refresh(s, SETTINGS, force=True)
        st = await post_state(s, plan.sheet_id, "30", create=True)
        st.claimed_at = datetime.now(UTC) - timedelta(minutes=10)  # the bot died mid-send
        await s.commit()
        await tick(ch.bot, s, SETTINGS)
    assert to_channel(ch) == []
    assert any("перезапустился во время отправки" in t for t in texts(ch, ADMIN))
    await ch.press(EDITOR, ChannelCb(action="pub_go", no="30").pack())  # someone checked the channel
    assert [m.text for m in to_channel(ch)] == ["Ikki xil to‘g‘ri qaror bor"]
