"""The Python matcher must agree with the Apps Script (bot/Code.gs, cases of bot/test/run.js)."""

import pytest

from app.services.team import Person, build_matcher, match_people, split_list


def person(row: int, role: str, **kw) -> Person:
    return Person(
        row=row, role=role, name=kw.get("name", ""), aliases=split_list(kw.get("aliases")),
        generic=split_list(kw.get("generic")), products=split_list(kw.get("products")),
        username=kw.get("username", ""), chat_id=kw.get("chat_id", ""),
    )


PEOPLE = [
    person(2, "Руководитель", name="Анвар", aliases="анвара"),
    person(3, "Маркетолог", name="Лола"),
    person(4, "Монтажёр"),
    person(5, "Монтажёр №2"),
    person(6, "Методист BMC", generic="методист", products="BMC"),
    person(7, "Методист PM", generic="методист", products="PM"),
    person(8, "Руководитель маркетинга (Бобур)", name="Бобур", aliases="руководитель маркетинга; бобура"),
    person(9, "Темур", name="Темур", aliases="темура"),
]
MATCHER = build_matcher(PEOPLE)


def roles(text: str, product: str = "") -> str:
    return " | ".join(sorted(p.role for p in match_people(text, product, MATCHER)[0]))


@pytest.mark.parametrize(
    ("text", "product", "expected"),
    [
        ("Монтажёр №2", "BMC", "Монтажёр №2"),  # the longer name wins
        ("Монтажер + Монтажёр №2", "BMC", "Монтажёр | Монтажёр №2"),  # ё = е
        ("Методист", "PM", "Методист PM"),  # a generic name narrows down by the task's product
        ("Методист", "Школа", "Методист BMC | Методист PM"),  # no such product: everyone with that name
        ("Claude, проверка Анвара", "PM", "Руководитель"),  # a declension from «Другие названия»
        ("Руководитель маркетинга (Бобур)", "BMC", "Руководитель маркетинга (Бобур)"),
        ("Темур: решения", "BMC", "Темур"),  # C/I cells carry notes after a colon
        ("Хаби ака: согласование; Продакшн: бронь", "ММ", ""),
    ],
)
def test_match_like_apps_script(text, product, expected):
    assert roles(text, product) == expected


def test_rest_shows_what_matched_nobody():
    assert match_people("Анвар + комиссия", "BMC", MATCHER)[1] == "комиссия"


def test_person_key_is_one_per_chat():
    a = person(2, "Темур", chat_id="604")
    b = person(3, "Руководитель проекта", chat_id="604")
    c = person(4, "Продавцы", username="группа")
    assert a.key == b.key == "c604" and c.key == "r4"
