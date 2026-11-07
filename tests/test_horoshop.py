"""Тесты клиента Хорошоп и CLI horoshop_sync с подменённым HTTP-транспортом."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import horoshop_sync as hs  # noqa: E402
import product_names as pn  # noqa: E402
from horoshop_client import CategoryTree, HoroshopClient, HoroshopError  # noqa: E402

PAGES = [
    {"id": 10, "parent": 0, "title": {"ua": "Напої", "ru": "Напитки"}},
    {"id": 11, "parent": 10, "title": {"ua": "Соки", "ru": "Соки"}},
    {"id": 12, "parent": 11, "title": {"ua": "Яблучні", "ru": "Яблочные"}},
    {"id": 20, "parent": 0, "title": {"ua": "Техніка", "ru": "Техника"}},
]


class FakeHoroshop:
    """Имитация сервера: auth, pages/export (всё дерево или по уровням), catalog/export, catalog/import."""

    def __init__(self, pages=PAGES, nested=True, products=None, token_valid=True):
        self.pages = pages
        self.nested = nested
        self.products = products or []
        self.token_valid = token_valid
        self.calls: list[tuple[str, dict]] = []
        self.imported: list[dict] = []

    def __call__(self, url, body, headers):
        func = url.split("/api/")[1].strip("/")
        self.calls.append((func, body))
        assert headers["Content-Type"].startswith("application/json")
        if func == "auth":
            if body["password"] != "secret":
                return {"status": "ERROR", "response": {"message": "Невірний логін або пароль"}}
            return {"status": "OK", "response": {"token": "t" * 32}}
        if body.get("token") != "t" * 32:
            return {"status": "UNAUTHORIZED", "response": {"message": "bad token"}}
        if func == "pages/export":
            parent = body.get("parent", 0)
            if self.nested:
                pages = self.pages if parent == 0 else [p for p in self.pages if p["parent"] == parent]
            else:
                pages = [p for p in self.pages if p["parent"] == parent]
            return {"status": "OK" if pages else "EMPTY", "response": {"pages": pages}}
        if func == "catalog/export":
            offset, limit = body["offset"], body["limit"]
            return {"status": "OK", "response": {"products": self.products[offset : offset + limit]}}
        if func == "catalog/import":
            self.imported.extend(body["products"])
            return {"status": "OK", "response": {"log": []}}
        return {"status": "ERROR", "response": {"message": f"unknown {func}"}}


def make_client(server: FakeHoroshop) -> HoroshopClient:
    return HoroshopClient("myshop.com.ua", "api", "secret", transport=server)


# --------------------------------------------------------------------------- #


def test_auth_and_token_reuse():
    server = FakeHoroshop()
    client = make_client(server)
    client.export_pages()
    client.export_pages()
    assert [f for f, _ in server.calls] == ["auth", "pages/export", "pages/export"]
    assert server.calls[1][1]["token"] == "t" * 32
    assert client.base_url == "https://myshop.com.ua/api/"


def test_auth_failure_is_readable():
    client = HoroshopClient("https://myshop.com.ua/", "api", "wrong", transport=FakeHoroshop())
    with pytest.raises(HoroshopError, match="Невірний логін"):
        client.export_pages()


def test_category_tree_from_nested_response():
    server = FakeHoroshop(nested=True)
    tree = make_client(server).export_category_tree()
    assert len(tree) == 4
    assert [f for f, _ in server.calls].count("pages/export") == 1  # всё дерево пришло одним ответом
    assert tree.path(12) == "Напої / Соки / Яблучні"
    assert tree.path(12, "ru") == "Напитки / Соки / Яблочные"
    assert tree.is_leaf(12) and not tree.is_leaf(10)
    assert sorted(tree.leaf_ids()) == [12, 20]


def test_category_tree_from_flat_responses_recurses():
    server = FakeHoroshop(nested=False)
    tree = make_client(server).export_category_tree()
    assert len(tree) == 4
    assert tree.path(12) == "Напої / Соки / Яблучні"
    assert [f for f, _ in server.calls].count("pages/export") >= 4


def test_tree_prompt_lines_save_load(tmp_path):
    tree = CategoryTree.from_pages(PAGES)
    lines = tree.to_prompt_lines().splitlines()
    assert lines[0].startswith("id |")
    assert "12 | Напої / Соки / Яблучні | Напитки / Соки / Яблочные" in lines
    tree.save(tmp_path / "cats.json")
    loaded = CategoryTree.load(tmp_path / "cats.json")
    assert loaded.digest() == tree.digest()
    assert "[12] Яблучні" in tree.pretty()


def test_export_all_products_paginates():
    products = [{"article": f"A{i}", "title": {"ua": f"Товар {i}", "ru": f"Товар {i}"}, "parent": {"id": 12, "path": "Напої / Соки / Яблучні"}, "brand": "B", "price": i} for i in range(7)]
    server = FakeHoroshop(products=products)
    rows = make_client(server).export_all_products(page_size=3)
    assert len(rows) == 7
    exports = [b for f, b in server.calls if f == "catalog/export"]
    assert [(b["offset"], b["limit"]) for b in exports] == [(0, 3), (3, 3), (6, 3)]


def test_import_products_and_payload_builder():
    rows = [
        {"article": "A1", "name_uk": "Сік Sandora яблучний 1 л", "name_ru": "Сок Sandora яблочный 1 л", "category_id": "12", "apply": "yes"},
        {"article": "A2", "name_uk": "Вода", "name_ru": "Вода", "category_id": "0", "apply": "yes"},
        {"article": "A3", "name_uk": "Пропуск", "name_ru": "Пропуск", "category_id": "12", "apply": ""},
        {"article": "", "name_uk": "Без артикула", "name_ru": "", "category_id": "12", "apply": "yes"},
    ]
    payload = hs.build_import_payload(rows)
    assert payload == [
        {"article": "A1", "title": {"ua": "Сік Sandora яблучний 1 л", "ru": "Сок Sandora яблочный 1 л"}, "parent": {"id": 12}},
        {"article": "A2", "title": {"ua": "Вода", "ru": "Вода"}},
    ]
    assert hs.build_import_payload(rows, titles=False) == [{"article": "A1", "parent": {"id": 12}}]

    server = FakeHoroshop()
    result = make_client(server).import_products(payload)
    assert result["status"] == "OK" and server.imported == payload


def test_cli_apply_dry_run_and_yes(tmp_path, capsys, monkeypatch):
    review = tmp_path / "review.csv"
    pn.write_products(
        [{"article": "A1", "name_uk": "Сік", "name_ru": "Сок", "category_id": 12, "apply": "yes"}], review
    )
    assert hs.main(["apply", "--in", str(review), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)[0]["article"] == "A1"

    server = FakeHoroshop()
    monkeypatch.setattr(hs, "make_client", lambda args: make_client(server))
    assert hs.main(["apply", "--in", str(review), "--yes"]) == 0
    assert server.imported[0]["parent"] == {"id": 12}


def test_cli_process_offline_with_categories_file(tmp_path, monkeypatch):
    cats = tmp_path / "categories.json"
    CategoryTree.from_pages(PAGES).save(cats)
    products = tmp_path / "products.csv"
    pn.write_products(
        [
            {"article": "A1", "title_ua": "сік sandora 1л", "title_ru": "сок sandora 1л", "parent": "Напої / Соки", "parent_id": 11, "brand": "Sandora", "price": 50},
            {"article": "A2", "title_ua": "ЧАЙНИК tefal 1,7л", "title_ru": "", "parent": "Техніка", "parent_id": 20, "brand": "Tefal", "price": 900},
        ],
        products,
    )
    out = tmp_path / "review.csv"
    monkeypatch.setattr(hs, "make_client", lambda args: pytest.fail("сайт не должен понадобиться"))
    code = hs.main(["process", "--in", str(products), "--categories", str(cats), "--out", str(out), "--offline"])
    assert code == 0
    rows = pn.read_products(out)
    assert {r["article"] for r in rows} == {"A1", "A2"}
    assert all("category_path_ru" in r and "apply" in r for r in rows)
    assert next(r for r in rows if r["article"] == "A1")["name_uk"] == "Сік sandora 1 л"


# --------------------------------------------------------------------------- #
# Форматы дерева и работа с одним разделом
# --------------------------------------------------------------------------- #


def test_tree_loads_parentid_catalog_and_nested_children(tmp_path):
    catalog = {"catalog": [{"id": p["id"], "parentId": p["parent"], "title": p["title"], "path": {"ua": []}} for p in PAGES]}
    (tmp_path / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    nested = {"id": 10, "parentId": 0, "title": PAGES[0]["title"], "children": [
        {"id": 11, "parentId": 10, "title": PAGES[1]["title"], "children": [
            {"id": 12, "parentId": 11, "title": PAGES[2]["title"], "children": []}]}]}
    (tmp_path / "nested.json").write_text(json.dumps(nested, ensure_ascii=False), encoding="utf-8")

    from_catalog = CategoryTree.load(tmp_path / "catalog.json")
    from_nested = CategoryTree.load(tmp_path / "nested.json")
    assert from_catalog.path(12) == "Напої / Соки / Яблучні"
    assert from_nested.path(12) == "Напої / Соки / Яблучні" and len(from_nested) == 3
    assert "children" not in from_nested.nodes[10].extra


def test_subtree_and_descendants():
    tree = CategoryTree.from_pages(PAGES)
    sub = tree.subtree(10)
    assert set(sub.nodes) == {11, 12} and sub.path(12) == "Соки / Яблучні"
    with_root = tree.subtree(10, include_root=True)
    assert set(with_root.nodes) == {10, 11, 12} and with_root.path(12) == "Напої / Соки / Яблучні"
    assert tree.descendant_ids(10) == {11, 12}
    with pytest.raises(KeyError):
        tree.subtree(999)


def test_cli_process_root_id_filters_products(tmp_path, monkeypatch):
    cats = tmp_path / "categories.json"
    CategoryTree.from_pages(PAGES).save(cats)
    products = tmp_path / "products.csv"
    pn.write_products(
        [
            {"article": "A1", "title_ua": "сік", "title_ru": "", "parent": "Напої / Соки", "parent_id": 11, "brand": "", "price": 1},
            {"article": "A2", "title_ua": "чайник", "title_ru": "", "parent": "Техніка", "parent_id": 20, "brand": "", "price": 2},
            {"article": "A3", "title_ua": "без розділу", "title_ru": "", "parent": "", "parent_id": "", "brand": "", "price": 3},
        ],
        products,
    )
    out = tmp_path / "review.csv"
    monkeypatch.setattr(hs, "make_client", lambda args: pytest.fail("сайт не должен понадобиться"))
    assert hs.main(["process", "--in", str(products), "--categories", str(cats), "--out", str(out), "--offline", "--root-id", "10"]) == 0
    assert {r["article"] for r in pn.read_products(out)} == {"A1", "A3"}
    assert hs.main(["process", "--in", str(products), "--categories", str(cats), "--out", str(out), "--offline", "--root-id", "10", "--all-products"]) == 0
    assert len(pn.read_products(out)) == 3


def test_normalize_domain_variants():
    for raw in ["soundsgood.com.ua", "https://soundsgood.com.ua/", "http://soundsgood.com.ua/admin/",
                "SoundsGood.com.ua/api", "https://soundsgood.com.ua/admin/?page=1"]:
        assert HoroshopClient.normalize_domain(raw) == "https://soundsgood.com.ua", raw
    with pytest.raises(HoroshopError):
        HoroshopClient.normalize_domain("https://")


def test_html_error_mentions_url_and_domain_hint(monkeypatch):
    import urllib.error
    import horoshop_client as hc

    class FakeResp:
        def read(self):
            return b"<html><body><h1>405 Not Allowed</h1></body></html>"

    def fake_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 405, "Method Not Allowed", {}, FakeResp())

    monkeypatch.setattr(hc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(HoroshopError) as info:
        hc.urllib_transport("https://wrong.host/api/auth/", {}, {})
    assert "wrong.host/api/auth/" in str(info.value) and "HOROSHOP_DOMAIN" in str(info.value)
    assert "<html>" not in str(info.value)


def test_check_connection():
    server = FakeHoroshop()
    assert "работает" in make_client(server).check_connection()


# --------------------------------------------------------------------------- #
# Прайс поставщика
# --------------------------------------------------------------------------- #


def make_supplier_xlsx(path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["ООО Поставщик", None, None, None])          # шапка прайса
    ws.append(["Прайс-лист от 01.11.2026", None, None, None])
    ws.append([None, None, None, None])
    ws.append(["Код", "Наименование", "Бренд", "Цена, грн"])  # заголовки в 4-й строке
    ws.append([None, "Синтезаторы", None, None])             # заголовок группы
    ws.append(["SKU-1", "синтезатор yamaha psr-e373 61 клавиша", "Yamaha", "12 990"])
    ws.append(["SKU-2", "CASIO CT-S300 синтезатор 61кл", "Casio", 8490])
    ws.append([None, "Цифровые пианино", None, None])         # ещё группа
    ws.append(["SKU-3", "цифровое пианино kawai es120 чёрное", "Kawai", "39 900,00"])
    ws.append([None, None, None, None])
    wb.save(path)


def test_supplier_header_detection_and_groups(tmp_path):
    path = tmp_path / "price.xlsx"
    make_supplier_xlsx(path)
    rows = pn.read_products(path)
    assert list(rows[0].keys()) == ["Код", "Наименование", "Бренд", "Цена, грн"]
    items = hs.prepare_supplier_rows(rows, name_col="Наименование", article_col="Код", brand_col="Бренд",
                                     price_col="Цена, грн", category_col=None)
    assert [i["article"] for i in items] == ["SKU-1", "SKU-2", "SKU-3"]
    assert [i["supplier_category"] for i in items] == ["Синтезаторы", "Синтезаторы", "Цифровые пианино"]
    assert items[2]["price"] == "39 900,00" and items[1]["price"] == "8490"


def test_cli_supplier_offline_and_create_payload(tmp_path, monkeypatch):
    path = tmp_path / "price.xlsx"
    make_supplier_xlsx(path)
    cats = tmp_path / "categories.json"
    CategoryTree.from_pages(PAGES).save(cats)
    out = tmp_path / "review_supplier.csv"
    monkeypatch.setattr(hs, "make_client", lambda args: pytest.fail("сайт не должен понадобиться"))
    assert hs.main(["supplier", "--in", str(path), "--categories", str(cats), "--out", str(out), "--offline"]) == 0
    rows = pn.read_products(out)
    assert {r["article"] for r in rows} == {"SKU-1", "SKU-2", "SKU-3"}
    row = next(r for r in rows if r["article"] == "SKU-2")
    assert row["name_uk"] == "CASIO CT-S300 синтезатор 61кл" and row["supplier_category"] == "Синтезаторы"
    assert row["apply"] == ""  # офлайн: раздел не подобран -> не отправлять

    # Имитация проверенного файла: раздел проставлен, apply=yes -> payload с брендом и ценой
    for r in rows:
        r["category_id"] = 12
        r["apply"] = "yes"
    payload = hs.build_import_payload(rows, create=True)
    assert payload[0]["parent"] == {"id": 12} and payload[0]["brand"] in ("Yamaha", "Casio", "Kawai")
    assert all(isinstance(p["price"], float) for p in payload)
    assert next(p for p in payload if p["article"] == "SKU-3")["price"] == 39900.0
    # без раздела новый товар не создаётся
    rows[0]["category_id"] = 0
    assert len(hs.build_import_payload(rows, create=True)) == 2


def test_detect_header_row_falls_back_to_first_nonempty():
    raw = [[None, None], ["Шапка"], ["a", "b", "c"], [1, 2, 3]]
    assert pn.detect_header_row(raw) == 2
    assert pn.detect_header_row([["Артикул", "Назва", "Ціна"], [1, "x", 2]]) == 0


# --------------------------------------------------------------------------- #
# Выгрузка админпанели, поставщики, путь раздела -> id
# --------------------------------------------------------------------------- #

ADMIN_ROWS = [
    {"Артикул": "A1", "Родительский артикул": "A1", "Название модификации (UA)": "", "Название модификации (RU)": "",
     "Название (UA)": "сік sandora 1л", "Название (RU)": "сок sandora 1л", "Бренд": "Sandora", "Раздел": "Напої/Соки/Яблучні",
     "Цена": "54.90", "Валюта": "UAH", "Отображать": "Да", "Наявність": "В наявності", "Поставщик": "Thomann", "Ссылка": "https://x/a1"},
    {"Артикул": "A2", "Родительский артикул": "A2", "Название модификации (UA)": "", "Название модификации (RU)": "",
     "Название (UA)": "чайник", "Название (RU)": "", "Бренд": "Tefal", "Раздел": "Техніка", "Цена": "900", "Валюта": "UAH",
     "Отображать": "Нет", "Наявність": "Немає", "Поставщик": "Komora", "Ссылка": ""},
    {"Артикул": "A3", "Родительский артикул": "A3", "Название модификации (UA)": "", "Название модификации (RU)": "",
     "Название (UA)": "сік", "Название (RU)": "", "Бренд": "", "Раздел": "Каталог/Напої/Соки", "Цена": "20", "Валюта": "UAH",
     "Отображать": "Да", "Наявність": "В наявності", "Поставщик": "", "Ссылка": ""},
]


def test_admin_export_is_recognized_and_normalized():
    assert hs.is_admin_export(ADMIN_ROWS[0])
    rows = hs.normalize_admin_export(ADMIN_ROWS)
    assert rows[0]["title_ua"] == "сік sandora 1л" and rows[0]["supplier"] == "Thomann" and rows[0]["parent"] == "Напої/Соки/Яблучні"
    assert rows[0]["display_in_showcase"] == 1 and rows[1]["display_in_showcase"] == 0


def test_supplier_filter_and_visibility():
    rows = hs.normalize_admin_export(ADMIN_ROWS)
    assert [r["article"] for r in hs.filter_rows(rows, supplier="thomann", only_visible=False)] == ["A1"]
    assert [r["article"] for r in hs.filter_rows(rows, supplier="Thomann, Komora", only_visible=False)] == ["A1", "A2"]
    assert [r["article"] for r in hs.filter_rows(rows, supplier="(пусто)", only_visible=False)] == ["A3"]
    assert [r["article"] for r in hs.filter_rows(rows, supplier=None, only_visible=True)] == ["A1", "A3"]


def test_resolve_path_variants():
    tree = CategoryTree.from_pages([{"id": 1, "parent": 0, "title": {"ua": "Каталог", "ru": "Каталог"}}] +
                                   [dict(p, parent=(1 if p["parent"] == 0 else p["parent"])) for p in PAGES])
    assert tree.resolve_path("Напої/Соки/Яблучні") == 12
    assert tree.resolve_path("Каталог/Напої/Соки/Яблучні") == 12
    assert tree.resolve_path("напої / соки") == 11
    assert tree.resolve_path("Немає такого") is None
    assert tree.resolve_path("") is None


def test_cli_process_admin_export_with_supplier_and_root(tmp_path, monkeypatch):
    cats = tmp_path / "categories.json"
    CategoryTree.from_pages(PAGES).save(cats)
    products = tmp_path / "export.csv"
    pn.write_products(ADMIN_ROWS, products)
    out = tmp_path / "review.csv"
    monkeypatch.setattr(hs, "make_client", lambda args: pytest.fail("сайт не должен понадобиться"))
    code = hs.main(["process", "--in", str(products), "--categories", str(cats), "--out", str(out), "--offline",
                    "--root-id", "10", "--supplier", "Thomann,(пусто)"])
    assert code == 0
    rows = pn.read_products(out)
    assert {r["article"] for r in rows} == {"A1", "A3"}          # A2: другой поставщик и другой раздел
    assert {r["parent_id"] for r in rows} == {"12", "11"}         # путь раздела сопоставлен с id (и с лишним «Каталог/»)
    assert hs.main(["suppliers", "--in", str(products)]) == 0


def test_cli_supplier_handles_admin_export(tmp_path, monkeypatch):
    cats = tmp_path / "categories.json"
    CategoryTree.from_pages(PAGES).save(cats)
    products = tmp_path / "export.csv"
    pn.write_products(ADMIN_ROWS, products)
    out = tmp_path / "review_supplier.csv"
    monkeypatch.setattr(hs, "make_client", lambda args: pytest.fail("сайт не должен понадобиться"))
    code = hs.main(["supplier", "--in", str(products), "--categories", str(cats), "--out", str(out), "--offline",
                    "--supplier", "Thomann"])
    assert code == 0
    rows = pn.read_products(out)
    assert [r["article"] for r in rows] == ["A1"]
    assert rows[0]["source_name"] == "сік sandora 1л" and rows[0]["supplier_category"] == "Напої/Соки/Яблучні"
