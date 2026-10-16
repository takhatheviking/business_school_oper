"""The one-message course form, in the same format as the event form (see event_form.py):

    [Название RU]
    Финансы для собственников
    [Расписание RU]
    пт и вс, 10:00–18:00 · 17 учебных дней
    [Старт]
    20.11.2026
    [Стоимость]
    -

Only the Russian title is required. An empty start date means "dates to be announced",
an empty price means "price on request" (the lead goes to sales either way).
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from app.db.models import Course
from app.formatting import esc, parse_money_to_tiyin
from app.i18n import LANGS, t
from app.services.event_form import HEADER_RE, LANG_TAG, SKIP, Section, _norm

TEXT_FIELDS = ("title", "description", "schedule")
LIMITS = {"title": 255, "description": 3500, "schedule": 255}


def sections() -> list[Section]:
    out = [Section(f"{f}.{lang}", f"form_{f}", lang) for f in TEXT_FIELDS for lang in LANGS]
    return out + [Section("starts_on", "form_starts_on"), Section("price", "form_course_price")]


def header(section: Section, ui_lang: str) -> str:
    label = t(ui_lang, section.label_key)
    return f"[{label} {LANG_TAG[section.lang]}]" if section.lang else f"[{label}]"


def label_of(key: str, ui_lang: str) -> str:
    return header(next(s for s in sections() if s.key == key), ui_lang)[1:-1]


def _label_index() -> dict[str, Section]:
    return {_norm(header(s, ui)[1:-1]): s for s in sections() for ui in LANGS}


def blank_form(ui_lang: str) -> str:
    return "\n\n".join(header(s, ui_lang) for s in sections())


def filled_form(course: Course, ui_lang: str) -> str:
    values = {
        f"{f}.{lang}": (getattr(course, f) if lang == "ru" else (course.i18n or {}).get(lang, {}).get(f))
        for f in TEXT_FIELDS
        for lang in LANGS
    }
    values["starts_on"] = course.starts_on.strftime("%d.%m.%Y") if course.starts_on else None
    values["price"] = str(course.price_tiyin // 100) if course.price_tiyin is not None else None
    return "\n\n".join(f"{header(s, ui_lang)}\n{values.get(s.key) or '-'}" for s in sections())


def parse_date(text: str) -> date | None:
    cleaned = text.strip().replace("/", ".").replace("-", ".")
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(cleaned, pattern).date()
        except ValueError:
            continue
    return None


@dataclass
class CourseForm:
    texts: dict[str, dict[str, str | None]]  # field -> lang -> text
    starts_on: date | None
    price_tiyin: int | None
    missing: list[str] = field(default_factory=list)  # "description.en", ... (falls back to RU)

    def to_state(self) -> dict:
        return {
            "texts": self.texts,
            "starts_on": self.starts_on.isoformat() if self.starts_on else None,
            "price_tiyin": self.price_tiyin,
            "missing": self.missing,
        }

    @classmethod
    def from_state(cls, data: dict) -> "CourseForm":
        starts = data.get("starts_on")
        return cls(data["texts"], date.fromisoformat(starts) if starts else None, data.get("price_tiyin"), data.get("missing", []))

    def apply_to(self, course: Course) -> None:
        for f in TEXT_FIELDS:
            course.set_loc(f, self.texts[f])
        course.starts_on = self.starts_on
        course.price_tiyin = self.price_tiyin


def parse_form(text: str, *, ui_lang: str) -> tuple[CourseForm | None, list[str]]:
    """Returns the parsed form, or None and a list of human-readable errors."""
    errors: list[str] = []
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        return None, [t(ui_lang, "form_err_no_sections")]

    index = _label_index()
    raw: dict[str, str] = {}
    for i, m in enumerate(matches):
        section = index.get(_norm(m.group(1)))
        if section is None:
            errors.append(t(ui_lang, "form_err_unknown", label=esc(m.group(1))))
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[m.end():end].strip()
        raw[section.key] = "" if value in SKIP else value

    if not raw.get("title.ru"):
        errors.append(t(ui_lang, "form_err_missing", label=esc(label_of("title.ru", ui_lang))))

    texts: dict[str, dict[str, str | None]] = {f: {} for f in TEXT_FIELDS}
    missing = []
    for f in TEXT_FIELDS:
        for lang in LANGS:
            value = raw.get(f"{f}.{lang}") or None
            if value and len(value) > LIMITS[f]:
                errors.append(t(ui_lang, "form_err_too_long", label=esc(label_of(f"{f}.{lang}", ui_lang)), max=LIMITS[f]))
            if f == "title" and value and len(value) < 3:
                errors.append(t(ui_lang, "wiz_err_title"))
            texts[f][lang] = value
            if lang != "ru" and not value and texts[f].get("ru"):
                missing.append(f"{f}.{lang}")

    starts_on = None
    if raw.get("starts_on"):
        starts_on = parse_date(raw["starts_on"])
        if starts_on is None:
            errors.append(t(ui_lang, "course_err_date"))

    price = None
    if raw.get("price"):
        price = parse_money_to_tiyin(raw["price"])
        if price is None:
            errors.append(t(ui_lang, "wiz_err_price"))

    if errors:
        return None, list(dict.fromkeys(errors))
    return CourseForm(texts, starts_on, price, missing), []


EXAMPLE_VALUES = {
    "title.ru": "Финансы для собственников бизнеса",
    "title.uz": "Biznes egalari uchun moliya",
    "title.en": "Finance for business owners",
    "description.ru": "Практический курс: управленческий учёт, cash flow и юнит-экономика на цифрах вашей компании.\nФормат: офлайн, группа до 30 человек.",
    "description.uz": "Amaliy kurs: boshqaruv hisobi, cash flow va unit-iqtisodiyot — kompaniyangiz raqamlari asosida.\nFormat: oflayn, guruhda 30 kishigacha.",
    "description.en": "A hands-on course: management accounting, cash flow and unit economics on your company's numbers.\nFormat: in person, up to 30 people.",
    "schedule.ru": "пт и вс, 10:00–18:00 · 17 учебных дней",
    "schedule.uz": "juma va yakshanba, 10:00–18:00 · 17 oʻquv kuni",
    "schedule.en": "Fri and Sun, 10:00–18:00 · 17 class days",
    "starts_on": "20.11.2026",
    "price": "-",
}


def example_form(ui_lang: str) -> str:
    return "\n\n".join(f"{header(s, ui_lang)}\n{EXAMPLE_VALUES[s.key]}" for s in sections())
