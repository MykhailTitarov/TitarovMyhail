#!/usr/bin/env python3
"""
product_names.py — сортировка товаров и корректные названия на украинском
и русском языках через Claude API.

Что делает скрипт
-----------------
1. Читает список товаров из CSV или JSON (название, категория, бренд, цена,
   любые другие колонки сохраняются как есть).
2. Пакетами отправляет «сырые» названия в Claude API и получает для каждого
   товара грамотное название и категорию на украинском (name_uk, category_uk)
   и русском (name_ru, category_ru) языках, а также нормализованный бренд.
3. Кэширует ответы API в локальном JSON-файле, чтобы повторный запуск
   не тратил токены на уже обработанные товары.
4. Сортирует результат по любому набору полей (по умолчанию: категория,
   затем название на украинском) с правильным порядком букв кириллицы
   (украинский алфавит: ... Г Ґ Д Е Є ... И І Ї Й ...; русский: ... Е Ё ... Ъ Ы Ь Э Ю Я).
5. Записывает результат в CSV или JSON.
6. По желанию: проверяет написание брендов/моделей через веб-поиск (серверный инструмент
   web_search в Claude API) и подбирает категорию из дерева разделов сайта (например, Хорошоп).

Примеры
-------
    export ANTHROPIC_API_KEY=sk-ant-...
    python product_names.py --input products.csv --output products_sorted.csv
    python product_names.py -i products.json -o out.json --sort-by category_ru,-price
    python product_names.py -i products.csv -o out.csv --offline   # без API, только очистка и сортировка

Полное описание опций: python product_names.py --help
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Настройки по умолчанию
# --------------------------------------------------------------------------- #

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_BATCH_SIZE = 20
DEFAULT_WORKERS = 2
DEFAULT_SORT = "category_uk,name_uk"
DEFAULT_CACHE_FILE = ".product_names_cache.json"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Колонки входного файла, в которых скрипт ищет данные, если не задано явно.
NAME_COLUMNS = ("name", "title", "product", "назва", "название", "наименование", "товар")
CATEGORY_COLUMNS = ("category", "категорія", "категория", "група", "группа", "раздел", "розділ")
BRAND_COLUMNS = ("brand", "бренд", "виробник", "производитель", "manufacturer")
PRICE_COLUMNS = ("price", "ціна", "цена", "стоимость", "вартість")

OUTPUT_FIELDS = (
    "name_uk", "name_ru", "category_uk", "category_ru", "brand", "category_id", "category_path", "comment",
)

WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
DEFAULT_MAX_SEARCHES = 10  # максимум веб-поисков на один пакет товаров
MAX_PAUSE_TURNS = 8        # сколько раз продолжать ход после stop_reason == "pause_turn"

# Алфавиты для правильной сортировки кириллицы.
UK_ALPHABET = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
RU_ALPHABET = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"

# --------------------------------------------------------------------------- #
# Схема ответа модели (структурированный вывод)
# --------------------------------------------------------------------------- #


class NamedProduct(BaseModel):
    """Один товар в ответе модели."""

    index: int = Field(description="Номер товара из запроса (поле index), без изменений")
    name_uk: str = Field(description="Корректное название товара на украинском языке")
    name_ru: str = Field(description="Корректное название товара на русском языке")
    category_uk: str = Field(description="Категория товара на украинском языке")
    category_ru: str = Field(description="Категория товара на русском языке")
    brand: str = Field(description="Бренд в оригинальном написании или пустая строка")
    category_id: int = Field(
        description="id раздела из переданного дерева категорий сайта (самый глубокий подходящий). "
        "0 — если дерево не передано или подходящего раздела нет"
    )
    category_path: str = Field(
        description="Полный путь выбранного раздела на украинском, как в дереве (A / B / C); пустая строка, если category_id = 0"
    )
    comment: str = Field(
        description="Короткое замечание, если исходное название непонятно или "
        "содержит ошибку, которую нельзя исправить уверенно; иначе пустая строка"
    )


class NamingBatch(BaseModel):
    """Ответ модели на пакет товаров."""

    items: list[NamedProduct]


SYSTEM_PROMPT = """Ты — редактор каталога интернет-магазина. Тебе передают список товаров с «сырыми» названиями (из прайсов поставщиков, с опечатками, транслитом, смешением языков, лишними пробелами и КАПСОМ). Для каждого товара верни корректное, единообразное название и категорию на украинском и русском языках.

Правила оформления названий (одинаковы для обоих языков):
1. Структура: «Тип товара Бренд Модель/серия, ключевые характеристики (объём, вес, количество, цвет, размер)». Пример: «Смартфон Samsung Galaxy A55 8/256 ГБ, чёрный».
2. Тип товара пишется с заглавной буквы, остальные слова — со строчной, кроме имён собственных, брендов и моделей.
3. Бренды, модели и артикулы оставляй в оригинальном (обычно латинском) написании, не транслитерируй и не переводи: Samsung, Nivea, iPhone, Coca-Cola.
4. Единицы измерения — через пробел и в сокращённой форме языка: uk «мл, л, г, кг, шт., см, мм, Вт, ГБ», ru «мл, л, г, кг, шт., см, мм, Вт, ГБ». Десятичный разделитель — запятая (1,5 л).
5. Кавычки — только «ёлочки», и только там, где они действительно нужны (названия серий, вкусы: «Полуниця»).
6. Убирай мусор: дублирующиеся слова, коды поставщика, «!!!», «АКЦИЯ», «NEW», лишние пробелы, точки в конце.
7. Не выдумывай характеристики, которых нет в исходном названии. Если что-то непонятно — оставь как есть и опиши сомнение в поле comment.
8. Исправляй орфографию и грамматику; согласовывай падежи и род (ru: «чёрный», «чёрная»; uk: «чорний», «чорна»). В русском используй букву «ё». В украинском следи за буквами ґ, є, і, ї и апострофом (комп'ютер, м'ясо).
9. Украинское и русское название должны описывать один и тот же товар с одинаковым набором характеристик.
10. Категорию давай короткую (1–3 слова), в единственном или множественном числе так, как принято в каталогах: «Смартфони / Смартфоны», «Шампуні / Шампуни». Если категория задана во входных данных — используй её как подсказку и приведи к тому же стилю.

Отвечай строго по заданной JSON-схеме, по одному элементу на каждый входной товар, сохраняя поле index без изменений."""

WEB_CHECK_PROMPT = """Проверка написания через интернет. Тебе доступен инструмент web_search. Используй его, чтобы убедиться в точном официальном написании бренда, серии и модели (регистр, дефисы, пробелы: «Head & Shoulders», «iPhone 15 Pro Max», «Моршинська»), а также в корректных характеристиках, когда исходное название двусмысленно. Ищи только там, где есть сомнение; не трать поиск на очевидные товары (вода, хлеб, обычные позиции без бренда). Один запрос может покрыть несколько товаров одного бренда. Если поиск ничего не дал — оставь исходное написание и отметь это в comment."""

CATEGORY_PROMPT = """Раскладка по категориям сайта. Ниже — дерево разделов интернет-магазина в формате «id | путь ua | путь ru». Для каждого товара выбери САМЫЙ ГЛУБОКИЙ подходящий раздел (лист дерева, подподкатегорию, если она есть) и верни его id в category_id и путь ua в category_path (точно как в дереве). Поля category_uk / category_ru заполняй названием выбранного раздела (последний элемент пути) на соответствующем языке. Не придумывай id, которых нет в дереве. Если ни один раздел не подходит уверенно — category_id = 0, category_path пустая, а в comment предложи, какой раздел стоило бы создать. Текущий раздел товара во входных данных (поле category) — только подсказка: если он явно ошибочен, выбирай правильный.

Дерево разделов:
"""

# --------------------------------------------------------------------------- #
# Вспомогательные функции: чтение/запись, очистка, сортировка
# --------------------------------------------------------------------------- #


def read_products(path: Path, delimiter: str | None = None) -> list[dict[str, Any]]:
    """Читает товары из CSV или JSON. Возвращает список словарей."""
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            # Поддержка формата {"products": [...]} / {"items": [...]}
            for key in ("products", "items", "data"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise ValueError("JSON должен содержать список товаров или объект с ключом products/items/data")
        return [dict(row) for row in data]

    text = path.read_text(encoding="utf-8-sig")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [dict(row) for row in reader]


def write_products(rows: Sequence[dict[str, Any]], path: Path | None, delimiter: str = ",") -> None:
    """Пишет результат в CSV или JSON (по расширению). path=None — в stdout как CSV."""
    if path is not None and path.suffix.lower() == ".json":
        path.write_text(json.dumps(list(rows), ensure_ascii=False, indent=2), encoding="utf-8")
        return

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    def _dump(stream: io.TextIOBase) -> None:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, delimiter=delimiter, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fieldnames})

    if path is None:
        _dump(sys.stdout)
    else:
        # utf-8-sig — чтобы Excel сразу корректно открывал кириллицу.
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            _dump(fh)


def pick_column(row: dict[str, Any], explicit: str | None, candidates: Iterable[str]) -> str | None:
    """Возвращает имя колонки: явно заданное или первое подходящее из списка кандидатов."""
    if explicit:
        return explicit
    lowered = {k.strip().lower(): k for k in row}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


_UNIT_RE = re.compile(r"(?<=\d)(мл|л|г|кг|шт|см|мм|м|Вт|вт|ГБ|гб|ТБ|тб|ml|l|g|kg|gb|tb|pcs)\b", re.IGNORECASE)


def basic_cleanup(name: str) -> str:
    """Детерминированная очистка названия без обращения к API (для --offline).

    Убирает лишние пробелы, маркетинговый мусор, ставит «ёлочки», отделяет
    единицы измерения пробелом и делает первую букву заглавной.
    """
    s = str(name or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*[!]{2,}\s*", " ", s)
    s = re.sub(r"\b(АКЦИЯ|АКЦІЯ|NEW|НОВИНКА|SALE|ХИТ|ХІТ)\b[!]*", "", s, flags=re.IGNORECASE)
    s = re.sub(r'"([^"]+)"', r"«\1»", s)
    s = _UNIT_RE.sub(r" \1", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    if s.isupper() and len(s) > 3:
        s = s.capitalize()
    else:
        # Отдельные слова КАПСОМ на кириллице (ЙОГУРТ, ШАМПУНЬ) -> с заглавной буквы.
        # Латинские капс-слова не трогаем: это могут быть бренды (NIVEA, LG).
        s = re.sub(r"\b[А-ЯЁҐЄІЇ]{4,}\b", lambda m: m.group(0).capitalize(), s)
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s


def _alphabet_index(alphabet: str) -> dict[str, int]:
    return {ch: i for i, ch in enumerate(alphabet)}


_UK_INDEX = _alphabet_index(UK_ALPHABET)
_RU_INDEX = _alphabet_index(RU_ALPHABET)
_TOKEN_RE = re.compile(r"\d+|\D")


def cyrillic_sort_key(text: Any, lang: str = "uk") -> tuple:
    """Ключ сортировки для строк с кириллицей.

    Порядок: числа (по значению) < буквы кириллицы (по алфавиту языка) < латиница < остальное.
    Регистр не учитывается, числа внутри строк сравниваются как числа (A2 < A10).
    """
    index = _UK_INDEX if lang == "uk" else _RU_INDEX
    s = str(text or "").strip().lower()
    key: list[tuple] = []
    for token in _TOKEN_RE.findall(s):
        if token.isdigit():
            key.append((0, int(token), ""))
        elif token in index:
            key.append((1, index[token], ""))
        elif token in _UK_INDEX or token in _RU_INDEX:
            # Буква другого кириллического алфавита — ставим рядом с похожей по коду.
            key.append((1, len(index) + ord(token), ""))
        elif token.isalpha():
            key.append((2, ord(token), ""))
        elif token.isspace():
            key.append((0, -1, ""))  # пробел раньше всего, чтобы «Сік» < «Сік яблучний»
        else:
            key.append((3, ord(token), ""))
    return tuple(key)


def parse_price(value: Any) -> float:
    """Переводит цену из строки вида '1 299,50 грн' в число. Пусто/мусор -> +inf (в конец)."""
    if value is None:
        return float("inf")
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d,.\-]", "", str(value))
    if not s:
        return float("inf")
    if "," in s and "." in s:
        s = s.replace(",", "")  # 1,299.50
    else:
        s = s.replace(",", ".")  # 1299,50
    try:
        return float(s)
    except ValueError:
        return float("inf")


def sort_products(rows: list[dict[str, Any]], sort_by: str, price_col: str | None = None) -> list[dict[str, Any]]:
    """Сортирует товары по списку полей вида 'category_uk,name_uk,-price'.

    Поля name_ru/category_ru сортируются по русскому алфавиту, name_uk/category_uk — по украинскому,
    price (или колонка цены) — как число, остальные — как украинская кириллица/латиница.
    """
    specs: list[tuple[str, bool]] = []
    for raw in sort_by.split(","):
        raw = raw.strip()
        if not raw:
            continue
        desc = raw.startswith("-")
        specs.append((raw.lstrip("-+"), desc))

    def key_for(field: str) -> Callable[[dict[str, Any]], Any]:
        if field == "price" or (price_col and field == price_col):
            return lambda r: parse_price(r.get(field, r.get(price_col or field)))
        if field.endswith("_ru"):
            return lambda r: cyrillic_sort_key(r.get(field), "ru")
        return lambda r: cyrillic_sort_key(r.get(field), "uk")

    result = list(rows)
    # Устойчивая сортировка: применяем ключи от последнего к первому.
    for field, desc in reversed(specs):
        result.sort(key=key_for(field), reverse=desc)
    return result


# --------------------------------------------------------------------------- #
# Кэш ответов API
# --------------------------------------------------------------------------- #


class NameCache:
    """Простой JSON-кэш: ключ — хэш (модель + сырое название + категория + бренд)."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        if path and path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    @staticmethod
    def key(model: str, name: str, category: str, brand: str) -> str:
        raw = "\x1f".join([model, name.strip().lower(), category.strip().lower(), brand.strip().lower()])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        return self.data.get(key)

    def put(self, key: str, value: dict[str, Any]) -> None:
        self.data[key] = value

    def save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Обращение к Claude API
# --------------------------------------------------------------------------- #


def build_user_message(batch: list[dict[str, str]], notes: str | None = None) -> str:
    """Формирует текст запроса: JSON-список товаров с index/name/category/brand (+ заметки веб-проверки)."""
    payload = [
        {"index": item["index"], "name": item["name"], "category": item["category"], "brand": item["brand"]}
        for item in batch
    ]
    text = "Товары для обработки (JSON):\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    if notes:
        text += "\n\nРезультаты проверки написания через интернет (учитывай их):\n" + notes
    return text


def build_system(category_tree: Any = None, web_check: bool = False) -> list[dict[str, Any]]:
    """Системный промпт блоками: правила (+ веб-проверка) и дерево категорий. Оба блока кэшируются на стороне API."""
    rules = SYSTEM_PROMPT
    if web_check:
        rules += "\n\n" + WEB_CHECK_PROMPT
    blocks: list[dict[str, Any]] = [{"type": "text", "text": rules, "cache_control": {"type": "ephemeral"}}]
    if category_tree is not None and len(category_tree):
        blocks.append(
            {
                "type": "text",
                "text": CATEGORY_PROMPT + category_tree.to_prompt_lines(),
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks


def web_search_tool(max_uses: int) -> dict[str, Any]:
    return {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": max_uses}


def _raw_content(response: Any) -> list[dict[str, Any]]:
    """Содержимое ответа в виде, пригодном для отправки обратно (без служебного parsed_output)."""
    blocks = []
    for block in response.content:
        data = block.to_dict() if hasattr(block, "to_dict") else dict(block)
        data.pop("parsed_output", None)
        blocks.append(data)
    return blocks


def _check_stop(response: Any) -> None:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        reason = getattr(details, "explanation", None) or "без объяснения"
        raise RuntimeError(f"Модель отклонила запрос (refusal): {reason}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Ответ обрезан по max_tokens — уменьшите --batch-size")


def _is_tools_format_conflict(exc: BaseException) -> bool:
    """400 от API о несовместимости серверных инструментов и output_config.format."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    if not isinstance(exc, anthropic.BadRequestError):
        return False
    text = str(getattr(exc, "message", exc)).lower()
    return ("format" in text or "structured" in text) and ("tool" in text or "web_search" in text)


class ClaudeNamer:
    """Обёртка над Claude API: один запрос на пакет товаров с веб-проверкой и подбором категории."""

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        use_fallback: bool = True,
        web_check: bool = False,
        max_searches: int = DEFAULT_MAX_SEARCHES,
        category_tree: Any = None,
        log: Callable[[str], None] = lambda msg: None,
    ):
        self.client = client
        self.model = model
        self.effort = effort
        self.use_fallback = use_fallback
        self.web_check = web_check
        self.max_searches = max_searches
        self.category_tree = category_tree
        self.log = log
        self.two_step = False  # включается, если API не принимает веб-поиск вместе со строгой схемой
        self.system = build_system(category_tree, web_check)

    def _common_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=16000, system=self.system)
        if self.use_fallback:
            # Серверный fallback: если классификатор безопасности отклонит запрос,
            # API сам повторит его на рекомендованной запасной модели.
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _run_parse(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> Any:
        """parse() с продолжением хода при pause_turn (длинные серии веб-поисков)."""
        kwargs = self._common_kwargs()
        kwargs.update(messages=list(messages), output_format=NamingBatch, output_config={"effort": self.effort})
        if tools:
            kwargs["tools"] = tools
        for _ in range(MAX_PAUSE_TURNS + 1):
            response = self.client.beta.messages.parse(**kwargs)
            if response.stop_reason != "pause_turn":
                return response
            kwargs["messages"] = kwargs["messages"] + [{"role": "assistant", "content": _raw_content(response)}]
        raise RuntimeError("Модель не завершила ход после нескольких продолжений (pause_turn)")

    def _research_notes(self, batch: list[dict[str, str]]) -> str:
        """Шаг 1 двухэтапного режима: свободный текст с результатами веб-проверки."""
        kwargs = self._common_kwargs()
        kwargs.update(
            messages=[{"role": "user", "content": build_user_message(batch) + "\n\nПока НЕ формируй итоговые названия. Проверь через web_search спорные бренды/модели/характеристики и кратко перечисли по index, что удалось уточнить."}],
            tools=[web_search_tool(self.max_searches)],
            output_config={"effort": self.effort},
        )
        messages = kwargs["messages"]
        for _ in range(MAX_PAUSE_TURNS + 1):
            response = self.client.beta.messages.create(**kwargs)
            if response.stop_reason != "pause_turn":
                break
            kwargs["messages"] = messages = messages + [{"role": "assistant", "content": _raw_content(response)}]
        _check_stop(response)
        return "\n".join(b.text for b in response.content if b.type == "text").strip()

    def name_batch(self, batch: list[dict[str, str]]) -> list[NamedProduct]:
        user = {"role": "user", "content": build_user_message(batch)}
        if self.web_check and not self.two_step:
            try:
                response = self._run_parse([user], [web_search_tool(self.max_searches)])
            except Exception as exc:  # noqa: BLE001
                if not _is_tools_format_conflict(exc):
                    raise
                self.log("API не принимает web_search вместе со строгой схемой — переключаюсь на двухэтапный режим")
                self.two_step = True
        if self.web_check and self.two_step:
            notes = self._research_notes(batch)
            response = self._run_parse([{"role": "user", "content": build_user_message(batch, notes)}], None)
        elif not self.web_check:
            response = self._run_parse([user], None)

        _check_stop(response)
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError("Модель не вернула структурированный ответ")

        expected = {item["index"] for item in batch}
        got = {item.index for item in parsed.items}
        if got != expected:
            missing = sorted(expected - got)
            extra = sorted(got - expected)
            raise RuntimeError(f"Ответ не совпадает с запросом: нет index {missing}, лишние {extra}")
        return [self._validate_category(item) for item in parsed.items]

    def _validate_category(self, item: NamedProduct) -> NamedProduct:
        """id раздела должен существовать в дереве; иначе 0 + пометка в comment."""
        tree = self.category_tree
        if tree is None or not len(tree):
            if item.category_id or item.category_path:
                item.category_id, item.category_path = 0, ""
            return item
        if item.category_id and item.category_id in tree:
            item.category_path = tree.path(item.category_id, "ua")
            return item
        if item.category_id:
            note = f"модель вернула несуществующий раздел id={item.category_id}"
            item.comment = (item.comment + "; " if item.comment else "") + note
        item.category_id, item.category_path = 0, ""
        return item


def call_claude(client: Any, batch: list[dict[str, str]], model: str, effort: str, use_fallback: bool) -> list[NamedProduct]:
    """Один запрос к Claude для пакета товаров без веб-проверки и дерева категорий (совместимость)."""
    return ClaudeNamer(client, model=model, effort=effort, use_fallback=use_fallback).name_batch(batch)


def make_client() -> Any:
    """Создаёт клиент Anthropic. Ключ берётся из ANTHROPIC_API_KEY или профиля `ant auth login`."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Не установлен пакет anthropic: pip install -r requirements.txt") from exc
    return anthropic.Anthropic()


def describe_api_error(exc: BaseException) -> str:
    """Человекочитаемое описание ошибки API (от частного к общему)."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return "неверный или отсутствующий API-ключ (ANTHROPIC_API_KEY)"
    if isinstance(exc, anthropic.NotFoundError):
        return f"модель не найдена: {exc.message}"
    if isinstance(exc, anthropic.RateLimitError):
        retry = exc.response.headers.get("retry-after", "?")
        return f"превышен лимит запросов, повторите через {retry} с"
    if isinstance(exc, anthropic.BadRequestError):
        return f"некорректный запрос: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"ошибка API {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "нет соединения с API"
    return str(exc)


# --------------------------------------------------------------------------- #
# Основной конвейер
# --------------------------------------------------------------------------- #


def offline_result(item: dict[str, str]) -> dict[str, Any]:
    """Результат без API: очищенное название в обеих колонках, категория и бренд как есть."""
    cleaned = basic_cleanup(item["name"])
    category = basic_cleanup(item["category"]) if item["category"] else ""
    return {
        "name_uk": cleaned,
        "name_ru": cleaned,
        "category_uk": category,
        "category_ru": category,
        "brand": item["brand"].strip(),
        "category_id": 0,
        "category_path": "",
        "comment": "offline: название только очищено, не переведено",
    }


def process_products(
    rows: list[dict[str, Any]],
    *,
    name_col: str,
    category_col: str | None,
    brand_col: str | None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workers: int = DEFAULT_WORKERS,
    offline: bool = False,
    use_fallback: bool = True,
    web_check: bool = False,
    max_searches: int = DEFAULT_MAX_SEARCHES,
    category_tree: Any = None,
    cache: NameCache | None = None,
    client: Any = None,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> list[dict[str, Any]]:
    """Добавляет к каждому товару поля из OUTPUT_FIELDS: названия и категории ua/ru, бренд, id раздела, комментарий.

    web_check — проверять написание брендов/моделей веб-поиском (инструмент web_search Claude API).
    category_tree — CategoryTree (horoshop_client) для подбора category_id / category_path.
    """
    # Ключ кэша зависит от режима: смена дерева категорий или включение веб-проверки сбрасывает кэш.
    tree_digest = category_tree.digest() if category_tree is not None and len(category_tree) else ""
    cache_model = f"{model}|web={int(web_check)}|tree={tree_digest}"

    items: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        items.append(
            {
                "index": i,
                "name": str(row.get(name_col, "") or ""),
                "category": str(row.get(category_col, "") or "") if category_col else "",
                "brand": str(row.get(brand_col, "") or "") if brand_col else "",
            }
        )

    results: dict[int, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []

    for item in items:
        if not item["name"].strip():
            results[item["index"]] = {**{f: "" for f in OUTPUT_FIELDS}, "comment": "пустое название"}
            continue
        if offline:
            results[item["index"]] = offline_result(item)
            continue
        if cache is not None:
            cached = cache.get(NameCache.key(cache_model, item["name"], item["category"], item["brand"]))
            if cached:
                results[item["index"]] = cached
                continue
        pending.append(item)

    if pending:
        if client is None:
            client = make_client()
        batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
        mode = []
        if web_check:
            mode.append("веб-проверка")
        if tree_digest:
            mode.append(f"категории ({len(category_tree)} разделов)")
        log(
            f"Отправляю {len(pending)} товаров в {len(batches)} запросах к {model}"
            f" (из кэша: {len(items) - len(pending)}{'; ' + ', '.join(mode) if mode else ''})"
        )

        namer = ClaudeNamer(
            client, model=model, effort=effort, use_fallback=use_fallback, web_check=web_check,
            max_searches=max_searches, category_tree=category_tree, log=log,
        )

        def run(batch: list[dict[str, Any]]) -> list[NamedProduct]:
            return namer.name_batch(batch)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for n, (batch, outcome) in enumerate(zip(batches, pool.map(run, batches)), start=1):
                for named in outcome:
                    value = named.model_dump(exclude={"index"})
                    results[named.index] = value
                    if cache is not None:
                        src = next(it for it in batch if it["index"] == named.index)
                        cache.put(NameCache.key(cache_model, src["name"], src["category"], src["brand"]), value)
                log(f"  пакет {n}/{len(batches)} готов")
        if cache is not None:
            cache.save()

    enriched: list[dict[str, Any]] = []
    for item, row in zip(items, rows):
        new_row = dict(row)
        new_row.update({f: results[item["index"]].get(f, "") for f in OUTPUT_FIELDS})
        enriched.append(new_row)
    return enriched


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Сортировка товаров и корректные названия на украинском и русском через Claude API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Поля для --sort-by: name_uk, name_ru, category_uk, category_ru, brand, price "
            "и любая колонка входного файла. Префикс '-' — по убыванию.\n"
            "Пример: --sort-by category_ru,-price,name_ru"
        ),
    )
    p.add_argument("-i", "--input", required=True, type=Path, help="входной CSV или JSON с товарами")
    p.add_argument("-o", "--output", type=Path, help="выходной CSV или JSON (по умолчанию CSV в stdout)")
    p.add_argument("--name-col", help="колонка с названием (по умолчанию ищется автоматически)")
    p.add_argument("--category-col", help="колонка с категорией")
    p.add_argument("--brand-col", help="колонка с брендом")
    p.add_argument("--price-col", help="колонка с ценой (для сортировки по price)")
    p.add_argument("--sort-by", default=DEFAULT_SORT, help=f"поля сортировки (по умолчанию {DEFAULT_SORT})")
    p.add_argument("--no-sort", action="store_true", help="не сортировать, сохранить исходный порядок")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"модель Claude (по умолчанию {DEFAULT_MODEL})")
    p.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="товаров в одном запросе")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="параллельных запросов к API")
    p.add_argument("--cache", type=Path, default=Path(DEFAULT_CACHE_FILE), help="файл кэша ответов")
    p.add_argument("--no-cache", action="store_true", help="не использовать кэш")
    p.add_argument("--no-fallback", action="store_true", help="не включать серверный fallback-модели")
    p.add_argument("--offline", action="store_true", help="без API: только очистка названий и сортировка")
    p.add_argument("--web-check", action="store_true", help="проверять написание брендов/моделей веб-поиском")
    p.add_argument("--max-searches", type=int, default=DEFAULT_MAX_SEARCHES, help="максимум веб-поисков на пакет")
    p.add_argument("--categories", type=Path, help="JSON с деревом разделов сайта (horoshop_sync.py categories)")
    p.add_argument("--delimiter", help="разделитель CSV (по умолчанию определяется автоматически)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.input.exists():
        print(f"Файл не найден: {args.input}", file=sys.stderr)
        return 2

    rows = read_products(args.input, args.delimiter)
    if not rows:
        print("Во входном файле нет товаров", file=sys.stderr)
        return 2

    sample = rows[0]
    name_col = pick_column(sample, args.name_col, NAME_COLUMNS)
    if not name_col or name_col not in sample:
        print(
            f"Не найдена колонка с названием. Доступные колонки: {', '.join(sample)}. Укажите --name-col.",
            file=sys.stderr,
        )
        return 2
    category_col = pick_column(sample, args.category_col, CATEGORY_COLUMNS)
    brand_col = pick_column(sample, args.brand_col, BRAND_COLUMNS)
    price_col = pick_column(sample, args.price_col, PRICE_COLUMNS)

    cache = None if (args.no_cache or args.offline) else NameCache(args.cache)

    category_tree = None
    if args.categories:
        from horoshop_client import CategoryTree

        category_tree = CategoryTree.load(args.categories)

    try:
        enriched = process_products(
            rows,
            name_col=name_col,
            category_col=category_col,
            brand_col=brand_col,
            model=args.model,
            effort=args.effort,
            batch_size=args.batch_size,
            workers=args.workers,
            offline=args.offline,
            use_fallback=not args.no_fallback,
            web_check=args.web_check,
            max_searches=args.max_searches,
            category_tree=category_tree,
            cache=cache,
        )
    except Exception as exc:  # noqa: BLE001 — показываем понятную причину и выходим
        if cache is not None:
            cache.save()  # сохраняем уже полученные ответы
        print(f"Ошибка: {describe_api_error(exc)}", file=sys.stderr)
        return 1

    if not args.no_sort:
        enriched = sort_products(enriched, args.sort_by, price_col=price_col)

    write_products(enriched, args.output, delimiter=args.delimiter or ",")
    if args.output:
        print(f"Готово: {len(enriched)} товаров -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
