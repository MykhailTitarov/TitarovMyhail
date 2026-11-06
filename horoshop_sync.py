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

    # Товары: из CSV/JSON (шаг export) или прямо с сайта.
    if args.input:
        rows = pn.read_products(Path(args.input))
    else:
        if client is None:
            client = HoroshopClient.from_env(log=log)
        rows = [flatten_product(p) for p in client.export_all_products(parent_id=args.root_id or None)]
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
        if row.get("mod_title_ua") or row.get("mod_title_ru"):
            row["_source_name"] = f"{row['_source_name']} {row.get('mod_title_ua') or row.get('mod_title_ru')}".strip()

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


def build_import_payload(rows: Sequence[dict[str, Any]], *, titles: bool = True, categories: bool = True) -> list[dict[str, Any]]:
    """Строит products[] для catalog/import из строк review-файла."""
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
        if len(product) > 1:
            payload.append(product)
    return payload


def cmd_apply(args: argparse.Namespace, client: HoroshopClient | None) -> int:
    rows = pn.read_products(Path(args.input))
    payload = build_import_payload(rows, titles=not args.no_titles, categories=not args.no_categories)
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

    a = sub.add_parser("apply", help="загрузить review-файл в Хорошоп (catalog/import)")
    a.add_argument("--in", dest="input", required=True, help="review.csv после проверки")
    a.add_argument("--dry-run", action="store_true", help="показать payload и ничего не отправлять")
    a.add_argument("--yes", action="store_true", help="подтвердить отправку")
    a.add_argument("--preview", type=int, default=5, help="сколько товаров показать в предпросмотре")
    a.add_argument("--batch-size", type=int, default=IMPORT_BATCH_SIZE)
    a.add_argument("--no-titles", action="store_true", help="не обновлять названия")
    a.add_argument("--no-categories", action="store_true", help="не менять разделы")
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
        if args.command == "apply":
            return cmd_apply(args, make_client(args) if args.yes and not args.dry_run else None)
    except HoroshopError as exc:
        log(f"Ошибка Хорошоп API: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
