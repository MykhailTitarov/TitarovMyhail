"""Тесты для product_names.py. Запуск: pytest -q"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import product_names as pn  # noqa: E402

SAMPLE_CSV = Path(__file__).resolve().parents[1] / "examples" / "products_sample.csv"
SAMPLE_JSON = Path(__file__).resolve().parents[1] / "examples" / "products_sample.json"


# --------------------------------------------------------------------------- #
# Очистка и сортировка
# --------------------------------------------------------------------------- #


def test_basic_cleanup_removes_noise_and_fixes_units():
    assert pn.basic_cleanup("  шампунь   Head&Shoulders 400мл  !!!") == "Шампунь Head&Shoulders 400 мл"
    assert pn.basic_cleanup('Вода "Моршинська" 1,5л') == "Вода «Моршинська» 1,5 л"
    assert pn.basic_cleanup("АКЦИЯ!!! Гель NEW") == "Гель"
    assert pn.basic_cleanup("ЙОГУРТ") == "Йогурт"
    assert pn.basic_cleanup("ШАМПУНЬ Head&Shoulders NIVEA 400мл") == "Шампунь Head&Shoulders NIVEA 400 мл"
    assert pn.basic_cleanup("") == ""


def test_ukrainian_alphabet_order():
    words = ["Йогурт", "Ґудзик", "Гель", "Їжак", "Ікра", "Індик", "Єнот", "Жир", "Яблуко", "Авокадо"]
    ordered = sorted(words, key=pn.cyrillic_sort_key)
    assert ordered == ["Авокадо", "Гель", "Ґудзик", "Єнот", "Жир", "Ікра", "Індик", "Їжак", "Йогурт", "Яблуко"]


def test_russian_alphabet_order():
    words = ["Ёлка", "Ежевика", "Эклер", "Ягода", "Ыр", "Жир", "Йогурт", "Икра"]
    ordered = sorted(words, key=lambda w: pn.cyrillic_sort_key(w, "ru"))
    assert ordered == ["Ежевика", "Ёлка", "Жир", "Икра", "Йогурт", "Ыр", "Эклер", "Ягода"]


def test_sort_key_is_case_insensitive_numeric_and_puts_latin_after_cyrillic():
    assert pn.cyrillic_sort_key("самсунг") == pn.cyrillic_sort_key("САМСУНГ")
    assert sorted(["Galaxy A10", "Galaxy A2"], key=pn.cyrillic_sort_key) == ["Galaxy A2", "Galaxy A10"]
    assert sorted(["Samsung", "Яблуко"], key=pn.cyrillic_sort_key) == ["Яблуко", "Samsung"]
    assert sorted(["Сік яблучний", "Сік"], key=pn.cyrillic_sort_key) == ["Сік", "Сік яблучний"]


def test_parse_price_formats():
    assert pn.parse_price("13 999") == 13999
    assert pn.parse_price("1 299,50 грн") == 1299.5
    assert pn.parse_price("1,299.50") == 1299.5
    assert pn.parse_price(42) == 42
    assert pn.parse_price("") == float("inf")
    assert pn.parse_price(None) == float("inf")


def test_sort_products_multi_key_and_descending():
    rows = [
        {"category_uk": "Напої", "name_uk": "Сік", "price": "50"},
        {"category_uk": "Молочні продукти", "name_uk": "Йогурт", "price": "30"},
        {"category_uk": "Напої", "name_uk": "Вода", "price": "20"},
        {"category_uk": "Напої", "name_uk": "Кава", "price": "800"},
    ]
    by_name = pn.sort_products(rows, "category_uk,name_uk")
    assert [r["name_uk"] for r in by_name] == ["Йогурт", "Вода", "Кава", "Сік"]

    by_price_desc = pn.sort_products(rows, "category_uk,-price")
    assert [r["name_uk"] for r in by_price_desc] == ["Йогурт", "Кава", "Сік", "Вода"]


def test_sort_by_price_uses_detected_price_column():
    rows = [{"name_uk": "A", "Цена": "100"}, {"name_uk": "B", "Цена": "5"}, {"name_uk": "C", "Цена": ""}]
    result = pn.sort_products(rows, "price", price_col="Цена")
    assert [r["name_uk"] for r in result] == ["B", "A", "C"]


# --------------------------------------------------------------------------- #
# Чтение файлов и определение колонок
# --------------------------------------------------------------------------- #


def test_read_csv_and_pick_columns():
    rows = pn.read_products(SAMPLE_CSV)
    assert len(rows) == 10
    assert pn.pick_column(rows[0], None, pn.NAME_COLUMNS) == "name"
    assert pn.pick_column(rows[0], None, pn.CATEGORY_COLUMNS) == "category"
    assert pn.pick_column(rows[0], None, pn.PRICE_COLUMNS) == "price"
    assert pn.pick_column(rows[0], "sku", pn.NAME_COLUMNS) == "sku"


def test_read_json_with_ukrainian_headers():
    rows = pn.read_products(SAMPLE_JSON)
    assert len(rows) == 3
    assert pn.pick_column(rows[0], None, pn.NAME_COLUMNS) == "назва"
    assert pn.pick_column(rows[0], None, pn.CATEGORY_COLUMNS) == "категорія"
    assert pn.pick_column(rows[0], None, pn.PRICE_COLUMNS) == "ціна"


# --------------------------------------------------------------------------- #
# Конвейер с подменённым клиентом API
# --------------------------------------------------------------------------- #


class FakeClient:
    """Имитирует client.beta.messages.parse: переводит названия по словарю."""

    def __init__(self, translations: dict[str, tuple[str, str]], stop_reason: str = "end_turn"):
        self.translations = translations
        self.stop_reason = stop_reason
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["messages"][0]["content"].split("\n", 1)[1])
        items = []
        for entry in payload:
            uk, ru = self.translations[entry["name"]]
            items.append(
                pn.NamedProduct(
                    index=entry["index"], name_uk=uk, name_ru=ru,
                    category_uk="Кат", category_ru="Кат", brand=entry["brand"], comment="",
                )
            )
        return SimpleNamespace(stop_reason=self.stop_reason, stop_details=None,
                               parsed_output=pn.NamingBatch(items=items))


TRANSLATIONS = {
    "сік sandora 1л": ("Сік Sandora яблучний 1 л", "Сок Sandora яблочный 1 л"),
    "йогурт danone 290г": ("Йогурт Danone полуниця 290 г", "Йогурт Danone клубника 290 г"),
    "вода моршинська 1,5л": ("Вода мінеральна «Моршинська» 1,5 л", "Вода минеральная «Моршинська» 1,5 л"),
}


def make_rows():
    return [{"name": n, "category": "x", "brand": "", "price": str(i)} for i, n in enumerate(TRANSLATIONS)]


def test_process_products_batches_and_adds_fields(tmp_path):
    client = FakeClient(TRANSLATIONS)
    cache = pn.NameCache(tmp_path / "cache.json")
    rows = make_rows()

    out = pn.process_products(
        rows, name_col="name", category_col="category", brand_col="brand",
        batch_size=2, workers=1, client=client, cache=cache, log=lambda m: None,
    )

    assert len(client.calls) == 2  # 3 товара при batch_size=2 -> 2 запроса
    assert [r["name_uk"] for r in out] == [t[0] for t in TRANSLATIONS.values()]
    assert [r["name_ru"] for r in out] == [t[1] for t in TRANSLATIONS.values()]
    assert out[0]["price"] == "0"  # исходные колонки сохранены

    call = client.calls[0]
    assert call["model"] == pn.DEFAULT_MODEL
    assert call["output_format"] is pn.NamingBatch
    assert call["fallbacks"] == "default"
    assert pn.FALLBACK_BETA in call["betas"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}

    # Повторный запуск берёт всё из кэша и не ходит в API.
    client2 = FakeClient({})
    cache2 = pn.NameCache(tmp_path / "cache.json")
    out2 = pn.process_products(
        rows, name_col="name", category_col="category", brand_col="brand",
        client=client2, cache=cache2, log=lambda m: None,
    )
    assert client2.calls == []
    assert out2 == out


def test_no_fallback_flag_omits_beta_params():
    client = FakeClient(TRANSLATIONS)
    pn.process_products(
        make_rows(), name_col="name", category_col=None, brand_col=None,
        client=client, use_fallback=False, log=lambda m: None,
    )
    assert "fallbacks" not in client.calls[0]
    assert "betas" not in client.calls[0]


def test_refusal_raises_readable_error():
    client = FakeClient(TRANSLATIONS, stop_reason="refusal")
    with pytest.raises(RuntimeError, match="refusal"):
        pn.process_products(make_rows(), name_col="name", category_col=None, brand_col=None,
                            client=client, log=lambda m: None)


def test_mismatched_indexes_raise():
    class BadClient(FakeClient):
        def _parse(self, **kwargs):
            resp = super()._parse(**kwargs)
            resp.parsed_output.items.pop()  # потеряли один товар
            return resp

    with pytest.raises(RuntimeError, match="не совпадает"):
        pn.process_products(make_rows(), name_col="name", category_col=None, brand_col=None,
                            client=BadClient(TRANSLATIONS), log=lambda m: None)


def test_empty_names_skip_api():
    client = FakeClient({})
    out = pn.process_products([{"name": ""}, {"name": "   "}], name_col="name", category_col=None,
                              brand_col=None, client=client, log=lambda m: None)
    assert client.calls == []
    assert all(r["comment"] == "пустое название" for r in out)


# --------------------------------------------------------------------------- #
# CLI: офлайн-режим end-to-end
# --------------------------------------------------------------------------- #


def test_cli_offline_csv_to_csv(tmp_path):
    out = tmp_path / "out.csv"
    code = pn.main(["-i", str(SAMPLE_CSV), "-o", str(out), "--offline", "--sort-by", "category,name_uk"])
    assert code == 0
    rows = pn.read_products(out)
    assert len(rows) == 10
    assert set(pn.OUTPUT_FIELDS) <= set(rows[0])
    categories = [r["category"] for r in rows]
    assert categories == sorted(categories, key=pn.cyrillic_sort_key)
    noisy = next(r for r in rows if r["id"] == "101")
    assert noisy["name_uk"] == "Смартфон samsung galaxy a55 8/256 gb черный"


def test_cli_offline_json_to_json_with_ukrainian_headers(tmp_path):
    out = tmp_path / "out.json"
    code = pn.main(["-i", str(SAMPLE_JSON), "-o", str(out), "--offline", "--sort-by=-price"])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [d["sku"] for d in data] == ["A-1", "A-3", "A-2"]
    assert data[0]["name_uk"] == "Кава lavazza qualita oro зерно 1 кг"


def test_cli_missing_name_column(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("sku,qty\n1,2\n", encoding="utf-8")
    assert pn.main(["-i", str(bad), "--offline"]) == 2
    assert "--name-col" in capsys.readouterr().err
