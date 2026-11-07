#!/usr/bin/env python3
"""
run.py — запуск всего конвейера через меню, без ручного ввода команд.

Первый запуск: скрипт спросит ключ Claude API, домен сайта, логин и пароль API Хорошоп
и сохранит их в файл .env рядом со скриптом (файл не попадает в git).
Дальше просто выбирайте пункты меню:

    1. Выгрузить дерево разделов сайта        -> categories.json
    2. Выгрузить товары                        -> products.csv
    3. Обработать товары через Claude          -> review.csv
    4. Показать, что будет загружено (dry-run)
    5. Загрузить review.csv на сайт
    6. Изменить настройки (.env)
    7. Проверка без ключей (пример, офлайн)

Запуск:  python run.py        (Windows: двойной клик по start.bat)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CATEGORIES_FILE = "categories.json"
PRODUCTS_FILE = "products.csv"
REVIEW_FILE = "review.csv"

SETTINGS = [
    # (переменная, подсказка, секрет?)
    ("ANTHROPIC_API_KEY", "Ключ Claude API (sk-ant-...)", True),
    ("HOROSHOP_DOMAIN", "Домен сайта на Хорошоп (например myshop.com.ua, без https:// и /api)", False),
    ("HOROSHOP_LOGIN", "Логин API-администратора Хорошоп", False),
    ("HOROSHOP_PASSWORD", "Пароль API-администратора Хорошоп", True),
]


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = ["# Настройки для product_names.py / horoshop_sync.py. Файл не попадает в git."]
    for key, hint, _secret in SETTINGS:
        lines.append(f"# {hint}")
        lines.append(f"{key}={values.get(key, '')}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def ask_settings(current: dict[str, str]) -> dict[str, str]:
    print("\nВведите настройки и нажимайте Enter. Вставка: Ctrl+V или правая кнопка мыши.")
    print("Вводимый текст виден на экране. Enter без ввода — оставить текущее значение.\n")
    values = dict(current)
    for key, hint, secret in SETTINGS:
        current_value = values.get(key, "")
        if secret and current_value:
            shown = current_value[:6] + "…" + current_value[-3:] if len(current_value) > 12 else "*" * len(current_value)
        else:
            shown = current_value or "не задано"
        while True:
            entered = input(f"{hint}\n  [{shown}]: ").strip()
            if entered:
                values[key] = entered
                break
            if current_value:
                break
            print("  Значение пустое. Введите его или нажмите Ctrl+C для выхода.")
    return values


def load_settings(force_ask: bool = False) -> dict[str, str]:
    values = read_env_file(ENV_FILE)
    missing = [key for key, _h, _s in SETTINGS if not values.get(key) and not os.environ.get(key)]
    if force_ask or missing:
        if missing and not force_ask:
            print("Не хватает настроек: " + ", ".join(missing))
        values = ask_settings(values)
        write_env_file(ENV_FILE, values)
        print(f"Сохранено в {ENV_FILE}\n")
    for key, value in values.items():
        if value and not os.environ.get(key):
            os.environ[key] = value
    return values


# --------------------------------------------------------------------------- #
# Действия меню
# --------------------------------------------------------------------------- #


def run_sync(argv: list[str]) -> int:
    import horoshop_sync

    print(f"\n> horoshop_sync.py {' '.join(argv)}\n")
    return horoshop_sync.main(argv)


def ask_yes(question: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(question + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def action_categories() -> int:
    return run_sync(["categories", "--out", CATEGORIES_FILE])


def action_export() -> int:
    return run_sync(["export", "--out", PRODUCTS_FILE])


def choose_supplier(products_path: str) -> str | None:
    """Показывает поставщиков из файла и даёт выбрать одного (номер или название)."""
    from collections import Counter

    import horoshop_sync

    rows = horoshop_sync.load_product_rows(Path(products_path))
    if not rows or "supplier" not in rows[0]:
        return None
    counts = Counter((str(r.get("supplier") or "").strip() or "(пусто)") for r in rows).most_common()
    print("\nПоставщики в файле:")
    for i, (name, n) in enumerate(counts, 1):
        print(f" {i:2}. {name:35} {n:6} товаров")
    answer = input("Номер или название поставщика (Enter — все поставщики): ").strip()
    if not answer:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(counts):
        return counts[int(answer) - 1][0]
    return answer


def action_process() -> int:
    products = input(f"Файл с товарами: products.csv из пункта 2 или выгрузка из админпанели (xlsx) [Enter — {PRODUCTS_FILE}]: ").strip().strip('"') or PRODUCTS_FILE
    if not Path(products).exists():
        print(f"Нет файла {products}. Выполните пункт 2 или укажите путь к выгрузке из админпанели.")
        return 2
    argv = ["process", "--in", products, "--categories", CATEGORIES_FILE, "--out", REVIEW_FILE]
    supplier = choose_supplier(products)
    if supplier:
        argv += ["--supplier", supplier]
    if ask_yes("Брать только товары с «Отображать = Да»?", default=True):
        argv.append("--only-visible")
    root = input("ID раздела, который обрабатываем (Enter — весь каталог; подсмотреть id можно в categories.json или в пункте 1): ").strip()
    if root.isdigit():
        argv += ["--root-id", root]
    limit = input("Сколько товаров обработать? (Enter — все, для пробы введите например 30): ").strip()
    if limit.isdigit() and int(limit) > 0:
        argv += ["--limit", limit]
    if ask_yes("Проверять написание брендов и моделей через интернет (дороже, но точнее)?", default=True):
        argv.append("--web-check")
    effort = input("Глубина работы модели: low / medium / high (Enter — medium): ").strip().lower()
    if effort in ("low", "medium", "high", "xhigh", "max"):
        argv += ["--effort", effort]
    code = run_sync(argv)
    if code == 0:
        print(f"\nОткройте {REVIEW_FILE} в Excel, проверьте названия и разделы, затем пункты 4 и 5.")
    return code


SUPPLIER_REVIEW_FILE = "review_supplier.csv"


def action_supplier() -> int:
    path = input("Путь к файлу поставщика (xlsx/csv), можно перетащить файл в окно: ").strip().strip('"')
    if not path or not Path(path).exists():
        print("Файл не найден.")
        return 2
    argv = ["supplier", "--in", path, "--categories", CATEGORIES_FILE, "--out", SUPPLIER_REVIEW_FILE]
    root = input("ID раздела сайта, куда раскладываем (Enter — весь каталог): ").strip()
    if root.isdigit():
        argv += ["--root-id", root]
    limit = input("Сколько товаров обработать? (Enter — все, для пробы например 30): ").strip()
    if limit.isdigit() and int(limit) > 0:
        argv += ["--limit", limit]
    if ask_yes("Проверять написание брендов и моделей через интернет?", default=True):
        argv.append("--web-check")
    code = run_sync(argv)
    if code == 0:
        print(f"\nОткройте {SUPPLIER_REVIEW_FILE}, проверьте названия и разделы. Загрузка на сайт — пункт 10.")
    return code


def action_apply_supplier() -> int:
    if not Path(SUPPLIER_REVIEW_FILE).exists():
        print(f"Нет файла {SUPPLIER_REVIEW_FILE}. Сначала выполните пункт 9.")
        return 2
    argv = ["apply", "--in", SUPPLIER_REVIEW_FILE, "--create"]
    run_sync(argv + ["--dry-run"])
    print("ВНИМАНИЕ: на сайте будут созданы новые товары по файлу review_supplier.csv (строки с apply=yes).")
    if not ask_yes("Отправить на сайт?"):
        print("Отменено.")
        return 0
    return run_sync(argv + ["--yes"])


def action_apply(dry_run: bool) -> int:
    if not Path(REVIEW_FILE).exists():
        print(f"Нет файла {REVIEW_FILE}. Сначала выполните пункт 3.")
        return 2
    argv = ["apply", "--in", REVIEW_FILE]
    if dry_run:
        return run_sync(argv + ["--dry-run"])
    print("ВНИМАНИЕ: названия и разделы товаров на сайте будут изменены по файлу review.csv.")
    if not ask_yes("Отправить на сайт?"):
        print("Отменено.")
        return 0
    return run_sync(argv + ["--yes"])


def action_check_connection() -> int:
    from horoshop_client import HoroshopClient, HoroshopError

    try:
        client = HoroshopClient.from_env(log=print)
        print(f"Домен: {os.environ.get('HOROSHOP_DOMAIN')}  ->  {client.base_url}")
        print(client.check_connection())
        return 0
    except HoroshopError as exc:
        print(f"Не удалось подключиться: {exc}")
        if exc.status == "ERROR":
            print("Проверьте логин и пароль API-администратора (пункт 6).")
        return 1


def action_offline_demo() -> int:
    import product_names

    print("\n> product_names.py -i examples/products_sample.csv --offline --sort-by category,name_uk\n")
    return product_names.main(["-i", str(BASE_DIR / "examples" / "products_sample.csv"), "--offline", "--sort-by", "category,name_uk"])


MENU = """
================  Товары: названия UA/RU и категории Хорошоп  ================
 1. Выгрузить дерево разделов сайта        -> {cats}
 2. Выгрузить товары с сайта               -> {prods}
 3. Обработать товары через Claude         -> {review}
 4. Показать, что будет загружено (без изменений на сайте)
 5. Загрузить {review} на сайт
 6. Изменить настройки (ключ, домен, логин, пароль)
 7. Проверка без ключей на примере (офлайн)
 8. Проверить подключение к Хорошоп (домен, логин, пароль)
 9. Прайс поставщика -> названия UA/RU и разделы сайта -> review_supplier.csv
10. Загрузить review_supplier.csv на сайт (создать новые товары)
 0. Выход
"""


def main() -> int:
    os.chdir(BASE_DIR)
    sys.path.insert(0, str(BASE_DIR))
    try:
        import anthropic  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError:
        print("Не установлены зависимости. Выполните:  python -m pip install -r requirements.txt")
        return 1

    load_settings()
    actions = {
        "1": action_categories,
        "2": action_export,
        "3": action_process,
        "4": lambda: action_apply(dry_run=True),
        "5": lambda: action_apply(dry_run=False),
        "6": lambda: (load_settings(force_ask=True), 0)[1],
        "7": action_offline_demo,
        "8": action_check_connection,
        "9": action_supplier,
        "10": action_apply_supplier,
    }
    while True:
        print(MENU.format(cats=CATEGORIES_FILE, prods=PRODUCTS_FILE, review=REVIEW_FILE))
        choice = input("Выберите пункт: ").strip()
        if choice == "0":
            return 0
        action = actions.get(choice)
        if action is None:
            print("Нет такого пункта.")
            continue
        try:
            code = action()
        except KeyboardInterrupt:
            print("\nПрервано.")
            code = 130
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка: {exc}")
            code = 1
        print("\nГотово." if code == 0 else f"\nЗавершено с ошибкой (код {code}).")
        input("Нажмите Enter, чтобы вернуться в меню...")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
