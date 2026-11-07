#!/usr/bin/env python3
"""
horoshop_sync.py — связка Хорошоп <-> Claude: выгрузить структуру сайта и товары,
получить правильные названия (ua/ru) с веб-проверкой, разложить по категориям
и загрузить изменения обратно через API.

Шаги (каждый — отдельная подкоманда, между шагами результат можно править руками):

    1. categories  — выгрузить дерево разделов сайта в categories.json (и показать его)
    2. export      — выгрузить товары в products.csv (артикул, названия ua/ru, раздел, бренд, цена)
    3. process     — прогнать товары через Claude: новые названия + подходящий раздел -> review.csv
    4. apply       — загрузить проверенный review.csv обратно: catalog/import (title.ua, title.ru, parent.id)

Доступ к API:
    export HOROSHOP_DOMAIN=myshop.com.ua         # домен сайта (без /api)
    export HOROSHOP_LOGIN=api_user               # логин API-администратора (Налаштування -> Адміни)
    export HOROSHOP_PASSWORD=...                 # пароль
    export ANTHROPIC_API_KEY=sk-ant-...          # для шага process

Примеры:
    python horoshop_sync.py categories --out categories.json
    python horoshop_sync.py export --out products.csv
    python horoshop_sync.py process --in products.csv --categories categories.json --out review.csv --web-check
    python horoshop_sync.py apply --in review.csv --dry-run      # показать, что будет отправлено
    python horoshop_sync.py apply --in review.csv --yes          # отправить
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

import product_names as pn
from horoshop_client import CategoryTree, HoroshopClient, HoroshopError, flatten_product

REVIEW_SORT = "category_path,name_uk"
IMPORT_BATCH_SIZE = 100

log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Подкоманды
# --------------------------------------------------------------------------- #


def cmd_categories(args: argparse.Namespace, client: HoroshopClient) -> int:
    tree = client.export_category_tree()
    if args.out:
        tree.save(args.out)
        log(f"Дерево разделов сохранено: {args.out} ({len(tree)} разделов, листьев: {len(tree.leaf_ids())})")
    print(tree.pretty())
    return 0


def cmd_export(args: argparse.Namespace, client: HoroshopClient) -> int:
    kwargs: dict[str, Any] = {}
    if args.only_visible:
        kwargs["display_in_showcase"] = 1
    if args.parent_id:
        kwargs["parent_id"] = args.parent_id
    products = client.export_all_products(page_size=args.page_size, included_params=args.fields, **kwargs)
    rows = [flatten_product(p) for p in products]
    if args.raw_json:
        Path(args.raw_json).write_text(json.dumps(products, ensure_ascii=False, indent=1), encoding="utf-8")
    pn.write_products(rows, args.out)
    log(f"Выгружено товаров: {len(rows)} -> {args.out or 'stdout'}")
    return 0


# Выгрузка товаров из админпанели Хорошоп (Товары -> Экспорт -> xlsx): колонки на русском.
ADMIN_EXPORT_MAP = {
    "article": "Артикул",
    "parent_article": "Родительский артикул",
    "title_ua": "Название (UA)",
    "title_ru": "Название (RU)",
    "mod_title_ua": "Название модификации (UA)",
    "mod_title_ru": "Название модификации (RU)",
    "brand": "Бренд",
    "parent": "Раздел",
    "price": "Цена",
    "currency": "Валюта",
    "display_in_showcase": "Отображать",
    "presence": "Наявність",
    "supplier": "Поставщик",
    "link": "Ссылка",
}


def is_admin_export(row: dict[str, Any]) -> bool:
    return "Артикул" in row and ("Название (UA)" in row or "Название (RU)" in row)


def normalize_admin_export(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Приводит выгрузку админпанели к колонкам, которые ждёт конвейер (как flatten_product)."""
    out = []
    for row in rows:
        rec = {key: ("" if row.get(src) is None else str(row.get(src)).strip()) for key, src in ADMIN_EXPORT_MAP.items()}
        rec["display_in_showcase"] = 1 if rec["display_in_showcase"].lower() in ("да", "так", "1", "yes", "true") else 0
        rec["parent_id"] = ""
        out.append(rec)
    return out


def load_product_rows(path: Path) -> list[dict[str, Any]]:
    rows = pn.read_products(path)
    if rows and is_admin_export(rows[0]):
        rows = normalize_admin_export(rows)
    return rows


def supplier_matches(value: Any, wanted: str) -> bool:
    """--supplier: список через запятую, без учёта регистра; '(пусто)' или '-' — товары без поставщика."""
    v = str(value or "").strip().lower()
    for w in (x.strip().lower() for x in wanted.split(",") if x.strip()):
        if w in ("(пусто)", "-", "none", "пусто") and not v:
            return True
        if v == w or (len(w) >= 3 and w in v):
            return True
    return False


def filter_rows(rows: list[dict[str, Any]], *, supplier: str | None, only_visible: bool) -> list[dict[str, Any]]:
    before = len(rows)
    if supplier:
        rows = [r for r in rows if supplier_matches(r.get("supplier"), supplier)]
        log(f"Поставщик «{supplier}»: {len(rows)} из {before}")
    if only_visible and rows and "display_in_showcase" in rows[0]:
        n = len(rows)
        rows = [r for r in rows if str(r.get("display_in_showcase", "1")) in ("1", "True", "true", "Да", "да")]
        log(f"Только отображаемые: {len(rows)} из {n}")
    return rows


def cmd_suppliers(args: argparse.Namespace, client: HoroshopClient | None) -> int:
    from collections import Counter

    rows = load_product_rows(Path(args.input))
    if not rows or "supplier" not in rows[0]:
        log("В файле нет колонки поставщика")
        return 2
    counts = Counter((str(r.get("supplier") or "").strip() or "(пусто)") for r in rows)
    visible = Counter((str(r.get("supplier") or "").strip() or "(пусто)") for r in rows if str(r.get("display_in_showcase", "1")) in ("1", "Да"))
    print(f"{'Поставщик':35} {'товаров':>8} {'отображ.':>8}")
    for name, n in counts.most_common():
        print(f"{name:35} {n:8} {visible.get(name, 0):8}")
    print(f"{'ВСЕГО':35} {len(rows):8}")
    return 0


def cmd_process(args: argparse.Namespace, client: HoroshopClient | None) -> int:
    # Дерево категорий: из файла или прямо с сайта.
    if args.categories and Path(args.categories).exists():
        tree = CategoryTree.load(args.categories)
    else:
        if client is None:
            client = HoroshopClient.from_env(log=log)
        tree = client.export_category_tree()
        if args.categories:
            tree.save(args.categories)
    if args.root_id:
        full_tree = tree
        tree = full_tree.subtree(args.root_id, include_root=True)
        log(f"Раздел {args.root_id} «{full_tree.nodes[args.root_id].title_ua}»: подразделов {len(tree) - 1}")
    log(f"Разделов в дереве: {len(tree)}")

    # Товары: из CSV/JSON/XLSX (шаг export или выгрузка админпанели) или прямо с сайта.
    if args.input:
        rows = load_product_rows(Path(args.input))
    else:
        if client is None:
            client = HoroshopClient.from_env(log=log)
        rows = [flatten_product(p) for p in client.export_all_products(parent_id=args.root_id or None)]
    rows = filter_rows(rows, supplier=args.supplier, only_visible=args.only_visible)
    # В выгрузке админпанели раздел задан путём, а не id: восстанавливаем id по дереву.
    if rows and "parent" in rows[0] and not any(str(r.get("parent_id") or "") for r in rows):
        unresolved = 0
        for r in rows:
            cid = full_tree.resolve_path(r.get("parent"))
            r["parent_id"] = cid if cid else ""
            unresolved += 0 if cid or not str(r.get("parent") or "").strip() else 1
        if unresolved:
            log(f"Не удалось сопоставить путь раздела с деревом у {unresolved} товаров")
    if args.root_id and args.input and not args.all_products and rows and "parent_id" in rows[0]:
        allowed = {str(i) for i in full_tree.descendant_ids(args.root_id)} | {str(args.root_id), ""}
        before = len(rows)
        rows = [r for r in rows if str(r.get("parent_id") or "") in allowed]
        log(f"Оставлено товаров раздела {args.root_id}: {len(rows)} из {before} (--all-products, чтобы взять все)")
    if not rows:
        log("Нет товаров для обработки")
        return 2
    if args.limit:
        rows = rows[: args.limit]

    # Исходное название: украинское, если есть, иначе русское, иначе колонка по умолчанию.
    for row in rows:
        row["_source_name"] = (row.get("title_ua") or row.get("title_ru") or row.get("title") or row.get("name") or "")
        mod = str(row.get("mod_title_ua") or row.get("mod_title_ru") or "").strip()
        if mod and mod.lower() not in str(row["_source_name"]).lower():
            row["_source_name"] = f"{row['_source_name']} {mod}".strip()

    cache = None if args.no_cache else pn.NameCache(Path(args.cache))
    try:
        enriched = pn.process_products(
            rows,
            name_col="_source_name",
            category_col="parent" if "parent" in rows[0] else None,
            brand_col="brand" if "brand" in rows[0] else None,
            model=args.model,
            effort=args.effort,
            batch_size=args.batch_size,
            workers=args.workers,
            offline=args.offline,
            use_fallback=not args.no_fallback,
            web_check=args.web_check and not args.offline,
            max_searches=args.max_searches,
            category_tree=tree,
            cache=cache,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        if cache is not None:
            cache.save()
        log(f"Ошибка: {pn.describe_api_error(exc)}")
        return 1

    for row in enriched:
        row.pop("_source_name", None)
        row["category_path_ru"] = tree.path(row["category_id"], "ru") if row.get("category_id") else ""
        current = str(row.get("parent_id") or "")
        row["category_changed"] = "yes" if row.get("category_id") and current and str(row["category_id"]) != current else ""
        row["apply"] = "yes" if row.get("name_uk") or row.get("category_id") else ""

    enriched = pn.sort_products(enriched, REVIEW_SORT)
    pn.write_products(enriched, args.out)
    unassigned = sum(1 for r in enriched if not r.get("category_id"))
    log(f"Готово: {len(enriched)} товаров -> {args.out or 'stdout'}; без категории: {unassigned}")
    log("Проверьте файл: колонка apply=yes отправится командой apply, пустая — будет пропущена.")
    return 0


def prepare_supplier_rows(
    rows: list[dict[str, Any]],
    *,
    name_col: str,
    article_col: str | None,
    brand_col: str | None,
    price_col: str | None,
    category_col: str | None,
) -> list[dict[str, Any]]:
    """Приводит прайс поставщика к единым колонкам: article, source_name, brand, price, supplier_category.

    Строка, где заполнено только название (нет артикула и цены), считается заголовком группы
    и становится supplier_category для следующих за ней товаров. Товары без названия пропускаются.
    """
    out: list[dict[str, Any]] = []
    group = ""
    for row in rows:
        name = str(row.get(name_col, "") or "").strip()
        article = str(row.get(article_col, "") or "").strip() if article_col else ""
        price = row.get(price_col, "") if price_col else ""
        price_text = str(price).strip() if price not in (None, "") else ""
        if not name:
            continue
        others = [str(v).strip() for k, v in row.items() if k != name_col and v not in (None, "")]
        if (article_col or price_col) and not article and not price_text and len(others) == 0:
            group = name
            continue
        out.append(
            {
                "article": article,
                "source_name": name,
                "brand": str(row.get(brand_col, "") or "").strip() if brand_col else "",
                "price": price_text,
                "supplier_category": (str(row.get(category_col, "") or "").strip() if category_col else "") or group,
                **{f"src_{k}": v for k, v in row.items() if k not in (name_col, article_col, brand_col, price_col, category_col)},
            }
        )
    return out


def cmd_supplier(args: argparse.Namespace, client: HoroshopClient | None) -> int:
    rows = pn.read_products(Path(args.input), sheet=args.sheet, header_row=args.header_row)
    if not rows:
        log("В файле поставщика нет строк с данными")
        return 2
    if is_admin_export(rows[0]):
        # Это выгрузка из админпанели Хорошоп, а не прайс поставщика: берём её родные колонки.
        log("Файл распознан как выгрузка из админпанели Хорошоп: название = «Название (UA)», раздел = «Раздел», поставщик = «Поставщик»")
        rows = normalize_admin_export(rows)
        for r in rows:
            r["name"] = r.get("title_ua") or r.get("title_ru") or ""
        rows = filter_rows(rows, supplier=args.supplier, only_visible=args.only_visible)
        args.name_col, args.article_col, args.brand_col, args.price_col, args.category_col = "name", "article", "brand", "price", "parent"
    sample = rows[0]
    name_col = pn.pick_column(sample, args.name_col, pn.NAME_COLUMNS)
    if not name_col or name_col not in sample:
        log(f"Не найдена колонка с названием. Колонки файла: {', '.join(sample)}. Укажите --name-col.")
        return 2
    article_col = pn.pick_column(sample, args.article_col, pn.ARTICLE_COLUMNS)
    brand_col = pn.pick_column(sample, args.brand_col, pn.BRAND_COLUMNS)
    price_col = pn.pick_column(sample, args.price_col, pn.PRICE_COLUMNS)
    category_col = pn.pick_column(sample, args.category_col, pn.CATEGORY_COLUMNS)
    log(
        "Колонки: название=«%s», артикул=«%s», бренд=«%s», цена=«%s», категория поставщика=«%s»"
        % (name_col, article_col or "-", brand_col or "-", price_col or "-", category_col or "-")
    )
    items = prepare_supplier_rows(
        rows, name_col=name_col, article_col=article_col, brand_col=brand_col, price_col=price_col, category_col=category_col
    )
    log(f"Товаров в прайсе: {len(items)} (строк в файле: {len(rows)})")
    if args.limit:
        items = items[: args.limit]

    if args.categories and Path(args.categories).exists():
        tree = CategoryTree.load(args.categories)
    else:
        if client is None:
            client = HoroshopClient.from_env(log=log)
        tree = client.export_category_tree()
        if args.categories:
            tree.save(args.categories)
    full_tree = tree
    if args.root_id:
        tree = full_tree.subtree(args.root_id, include_root=True)
        log(f"Раздел {args.root_id} «{full_tree.nodes[args.root_id].title_ua}»: подразделов {len(tree) - 1}")

    cache = None if args.no_cache else pn.NameCache(Path(args.cache))
    try:
        enriched = pn.process_products(
            items,
            name_col="source_name",
            category_col="supplier_category",
            brand_col="brand",
            model=args.model,
            effort=args.effort,
            batch_size=args.batch_size,
            workers=args.workers,
            offline=args.offline,
            use_fallback=not args.no_fallback,
            web_check=args.web_check and not args.offline,
            max_searches=args.max_searches,
            category_tree=tree,
            cache=cache,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001
        if cache is not None:
            cache.save()
        log(f"Ошибка: {pn.describe_api_error(exc)}")
        return 1

    for row in enriched:
        row["category_path_ru"] = full_tree.path(row["category_id"], "ru") if row.get("category_id") else ""
        row["apply"] = "yes" if row.get("article") and row.get("name_uk") and row.get("category_id") else ""
    enriched = pn.sort_products(enriched, REVIEW_SORT)
    pn.write_products(enriched, args.out)
    unassigned = sum(1 for r in enriched if not r.get("category_id"))
    no_article = sum(1 for r in enriched if not r.get("article"))
    log(f"Готово: {len(enriched)} товаров -> {args.out or 'stdout'}; без раздела: {unassigned}; без артикула: {no_article}")
    log("Проверьте файл. Загрузка на сайт: apply --in <файл> --create (создаёт новые товары с названием, разделом, брендом и ценой).")
    return 0


def build_import_payload(
    rows: Sequence[dict[str, Any]], *, titles: bool = True, categories: bool = True, create: bool = False
) -> list[dict[str, Any]]:
    """Строит products[] для catalog/import из строк review-файла.

    create=True добавляет бренд и цену (для новых товаров из прайса поставщика) и требует раздел.
    """
    payload: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("apply", "yes")).strip().lower() in ("", "no", "0", "false", "n"):
            continue
        article = str(row.get("article", "")).strip()
        if not article:
            continue
        product: dict[str, Any] = {"article": article}
        if titles and (row.get("name_uk") or row.get("name_ru")):
            title: dict[str, str] = {}
            if row.get("name_uk"):
                title["ua"] = str(row["name_uk"]).strip()
            if row.get("name_ru"):
                title["ru"] = str(row["name_ru"]).strip()
            product["title"] = title
        if categories and row.get("category_id") not in (None, "", 0, "0"):
            product["parent"] = {"id": int(row["category_id"])}
        if create:
            if "parent" not in product or "title" not in product:
                continue  # новый товар нельзя создать без названия и раздела
            if row.get("brand"):
                product["brand"] = str(row["brand"]).strip()
            price = pn.parse_price(row.get("price"))
            if price != float("inf"):
                product["price"] = price
        if len(product) > 1:
            payload.append(product)
    return payload


def cmd_apply(args: argparse.Namespace, client: HoroshopClient | None) -> int:
    rows = pn.read_products(Path(args.input))
    payload = build_import_payload(rows, titles=not args.no_titles, categories=not args.no_categories, create=args.create)
    if not payload:
        log("Нечего отправлять: нет строк с apply=yes и изменениями")
        return 2
    if args.dry_run or not args.yes:
        print(json.dumps(payload[: args.preview], ensure_ascii=False, indent=1))
        log(f"Будет обновлено товаров: {len(payload)} (показаны первые {min(args.preview, len(payload))}).")
        if not args.dry_run:
            log("Для отправки добавьте --yes")
        return 0

    if client is None:
        client = HoroshopClient.from_env(log=log)
    ok = warnings = 0
    for i in range(0, len(payload), args.batch_size):
        chunk = payload[i : i + args.batch_size]
        try:
            result = client.import_products(chunk)
        except HoroshopError as exc:
            log(f"Ошибка импорта пакета {i // args.batch_size + 1}: {exc}")
            return 1
        status = result.get("status")
        entries = (result.get("response") or {}).get("log") or []
        if status == "WARNING":
            warnings += 1
            for entry in entries:
                log(f"  предупреждение: {json.dumps(entry, ensure_ascii=False)[:300]}")
        ok += len(chunk)
        log(f"Пакет {i // args.batch_size + 1}: {status}, товаров {len(chunk)}")
    log(f"Отправлено товаров: {ok}; пакетов с предупреждениями: {warnings}")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Хорошоп <-> Claude: названия ua/ru и раскладка по категориям через API.")
    p.add_argument("--domain", help="домен сайта (или HOROSHOP_DOMAIN)")
    p.add_argument("--login", help="логин API (или HOROSHOP_LOGIN); пароль только через HOROSHOP_PASSWORD")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("categories", help="выгрузить дерево разделов")
    c.add_argument("--out", type=Path, default=Path("categories.json"))

    e = sub.add_parser("export", help="выгрузить товары")
    e.add_argument("--out", type=Path, default=Path("products.csv"))
    e.add_argument("--raw-json", help="дополнительно сохранить сырой ответ API в JSON")
    e.add_argument("--only-visible", action="store_true", help="только товары, включённые к показу")
    e.add_argument("--parent-id", type=int, help="только товары раздела с этим id")
    e.add_argument("--page-size", type=int, default=500, help="товаров за запрос (максимум 500)")
    e.add_argument("--fields", nargs="*", default=["title", "mod_title", "parent", "brand", "price", "display_in_showcase", "presence"],
                   help="поля includedParams для catalog/export")

    r = sub.add_parser("process", help="названия и категории через Claude -> review-файл")
    r.add_argument("--in", dest="input", help="CSV/JSON из шага export (иначе выгрузка с сайта)")
    r.add_argument("--categories", type=Path, default=Path("categories.json"), help="дерево разделов (если файла нет — выгрузится)")
    r.add_argument("--out", type=Path, default=Path("review.csv"))
    r.add_argument("--limit", type=int, help="обработать только первые N товаров (для пробы)")
    r.add_argument("--root-id", type=int, help="работать только с этим разделом и его подразделами (id из дерева)")
    r.add_argument("--supplier", help="только товары этого поставщика (можно несколько через запятую; '(пусто)' — без поставщика)")
    r.add_argument("--only-visible", action="store_true", help="только товары с «Отображать = Да»")
    r.add_argument("--all-products", action="store_true", help="с --root-id не отбрасывать товары других разделов")
    r.add_argument("--model", default=pn.DEFAULT_MODEL)
    r.add_argument("--effort", default=pn.DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--batch-size", type=int, default=pn.DEFAULT_BATCH_SIZE)
    r.add_argument("--workers", type=int, default=pn.DEFAULT_WORKERS)
    r.add_argument("--web-check", action="store_true", help="проверять написание брендов/моделей веб-поиском")
    r.add_argument("--max-searches", type=int, default=pn.DEFAULT_MAX_SEARCHES)
    r.add_argument("--cache", default=pn.DEFAULT_CACHE_FILE)
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--no-fallback", action="store_true")
    r.add_argument("--offline", action="store_true", help="без Claude: только очистка названий (категории не подбираются)")

    su = sub.add_parser("suppliers", help="список поставщиков в файле товаров с количеством позиций")
    su.add_argument("--in", dest="input", required=True, help="products.csv или выгрузка админпанели (xlsx)")

    sp = sub.add_parser("supplier", help="прайс поставщика -> названия ua/ru и разделы сайта -> review-файл")
    sp.add_argument("--in", dest="input", required=True, help="файл поставщика: xlsx, csv или json")
    sp.add_argument("--sheet", help="лист Excel (по умолчанию первый)")
    sp.add_argument("--header-row", type=int, help="номер строки с заголовками, с 0 (по умолчанию ищется автоматически)")
    sp.add_argument("--name-col", help="колонка с названием")
    sp.add_argument("--article-col", help="колонка с артикулом")
    sp.add_argument("--brand-col", help="колонка с брендом")
    sp.add_argument("--price-col", help="колонка с ценой")
    sp.add_argument("--category-col", help="колонка с категорией поставщика")
    sp.add_argument("--supplier", help="если файл — выгрузка админпанели: только товары этого поставщика")
    sp.add_argument("--only-visible", action="store_true", help="если файл — выгрузка админпанели: только «Отображать = Да»")
    sp.add_argument("--categories", type=Path, default=Path("categories.json"), help="дерево разделов сайта")
    sp.add_argument("--root-id", type=int, help="подбирать разделы только внутри этого раздела")
    sp.add_argument("--out", type=Path, default=Path("review_supplier.csv"))
    sp.add_argument("--limit", type=int, help="обработать только первые N товаров")
    sp.add_argument("--model", default=pn.DEFAULT_MODEL)
    sp.add_argument("--effort", default=pn.DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    sp.add_argument("--batch-size", type=int, default=pn.DEFAULT_BATCH_SIZE)
    sp.add_argument("--workers", type=int, default=pn.DEFAULT_WORKERS)
    sp.add_argument("--web-check", action="store_true", help="проверять написание брендов/моделей веб-поиском")
    sp.add_argument("--max-searches", type=int, default=pn.DEFAULT_MAX_SEARCHES)
    sp.add_argument("--cache", default=pn.DEFAULT_CACHE_FILE)
    sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--no-fallback", action="store_true")
    sp.add_argument("--offline", action="store_true", help="без Claude: только очистка названий")

    a = sub.add_parser("apply", help="загрузить review-файл в Хорошоп (catalog/import)")
    a.add_argument("--in", dest="input", required=True, help="review.csv после проверки")
    a.add_argument("--dry-run", action="store_true", help="показать payload и ничего не отправлять")
    a.add_argument("--yes", action="store_true", help="подтвердить отправку")
    a.add_argument("--preview", type=int, default=5, help="сколько товаров показать в предпросмотре")
    a.add_argument("--batch-size", type=int, default=IMPORT_BATCH_SIZE)
    a.add_argument("--no-titles", action="store_true", help="не обновлять названия")
    a.add_argument("--no-categories", action="store_true", help="не менять разделы")
    a.add_argument("--create", action="store_true", help="создавать новые товары (из прайса поставщика): + бренд и цена")
    return p


def make_client(args: argparse.Namespace) -> HoroshopClient:
    import os

    if args.domain:
        os.environ["HOROSHOP_DOMAIN"] = args.domain
    if args.login:
        os.environ["HOROSHOP_LOGIN"] = args.login
    return HoroshopClient.from_env(log=log)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "categories":
            return cmd_categories(args, make_client(args))
        if args.command == "export":
            return cmd_export(args, make_client(args))
        if args.command == "process":
            needs_site = not args.input or not (args.categories and Path(args.categories).exists())
            return cmd_process(args, make_client(args) if needs_site else None)
        if args.command == "suppliers":
            return cmd_suppliers(args, None)
        if args.command == "supplier":
            needs_site = not (args.categories and Path(args.categories).exists())
            return cmd_supplier(args, make_client(args) if needs_site else None)
        if args.command == "apply":
            return cmd_apply(args, make_client(args) if args.yes and not args.dry_run else None)
    except HoroshopError as exc:
        log(f"Ошибка Хорошоп API: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
