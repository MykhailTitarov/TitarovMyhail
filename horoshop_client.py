#!/usr/bin/env python3
"""
horoshop_client.py — минимальный клиент API Хорошоп (https://horoshop.notion.site/api-doc).

Покрывает функции, нужные для раскладки товаров по категориям и обновления названий:

    auth            POST https://<DOMAIN>/api/auth/            {login, password} -> response.token (живёт 600 с)
    pages/export    POST https://<DOMAIN>/api/pages/export/    {token, parent}   -> response.pages[] (дерево категорий)
    catalog/export  POST https://<DOMAIN>/api/catalog/export/  {token, offset, limit<=500, includedParams, article, parent}
    catalog/import  POST https://<DOMAIN>/api/catalog/import/  {token, products[]} -> status OK|WARNING|ERROR, response.log

Все запросы — JSON POST с заголовком Content-Type: application/json, как рекомендует документация.
Токен получается автоматически и обновляется до истечения срока жизни.

Данные для входа берутся из переменных окружения HOROSHOP_DOMAIN, HOROSHOP_LOGIN, HOROSHOP_PASSWORD
(логин/пароль API-администратора создаются в админпанели: Налаштування -> Адміни).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

TOKEN_TTL_SECONDS = 600
TOKEN_REFRESH_MARGIN = 60
EXPORT_PAGE_SIZE = 500  # документация: больше 500 за запрос выгрузить нельзя

# Транспорт: (url, json_body, headers) -> dict. Подменяется в тестах.
Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class HoroshopError(RuntimeError):
    """Ошибка API Хорошоп: статус не OK/EMPTY или HTTP-ошибка."""

    def __init__(self, status: str, message: str, response: Any = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.response = response


def urllib_transport(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float = 120.0) -> dict[str, Any]:
    """Отправляет JSON POST стандартной библиотекой и возвращает разобранный JSON."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8-sig", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise HoroshopError(
                "HTTP_ERROR",
                f"{exc.code} {exc.reason} по адресу {url}. Сервер ответил не JSON, а страницей — "
                f"проверьте домен сайта (HOROSHOP_DOMAIN): нужен домен самого магазина, например soundsgood.com.ua. "
                f"Начало ответа: {_strip_html(raw)[:200]}",
            ) from exc
    except urllib.error.URLError as exc:
        raise HoroshopError("CONNECTION_ERROR", f"{exc.reason} (адрес {url})") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HoroshopError("BAD_JSON", f"Ответ {url} не является JSON: {_strip_html(raw)[:200]}") from exc


def _strip_html(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


# --------------------------------------------------------------------------- #
# Дерево категорий
# --------------------------------------------------------------------------- #


@dataclass
class Category:
    id: int
    parent: int
    title_ua: str
    title_ru: str
    extra: dict[str, Any] = field(default_factory=dict)


class CategoryTree:
    """Дерево разделов сайта из pages/export: пути, листья, компактное описание для модели."""

    def __init__(self, categories: Iterable[Category]):
        self.nodes: dict[int, Category] = {c.id: c for c in categories}
        self.children: dict[int, list[int]] = {}
        for c in self.nodes.values():
            self.children.setdefault(c.parent, []).append(c.id)
        for ids in self.children.values():
            ids.sort(key=lambda i: (self.nodes[i].title_ua.lower(), i))

    # --- построение --------------------------------------------------------- #

    @staticmethod
    def _title(raw: Any, lang: str) -> str:
        if isinstance(raw, dict):
            value = raw.get(lang) or raw.get("uk" if lang == "ua" else lang) or ""
            if not value:
                # Фолбэк на любой непустой язык
                value = next((v for v in raw.values() if v), "")
            return str(value).strip()
        return str(raw or "").strip()

    @classmethod
    def from_pages(cls, pages: Iterable[dict[str, Any]]) -> "CategoryTree":
        """Строит дерево из списка разделов. Понимает ключи parent (Хорошоп) и parentId."""
        cats = []
        for p in pages:
            title = p.get("title", "")
            parent = p.get("parent") if "parent" in p else p.get("parentId")
            cats.append(
                Category(
                    id=int(p["id"]),
                    parent=int(parent or 0),
                    title_ua=cls._title(title, "ua"),
                    title_ru=cls._title(title, "ru"),
                    extra={k: v for k, v in p.items() if k not in ("id", "parent", "parentId", "title", "path", "children")},
                )
            )
        return cls(cats)

    @staticmethod
    def _flatten(node: dict[str, Any], out: list[dict[str, Any]]) -> None:
        out.append(node)
        for child in node.get("children") or []:
            CategoryTree._flatten(child, out)

    @classmethod
    def from_json_data(cls, data: Any) -> "CategoryTree":
        """Принимает любой из форматов: {"pages": [...]}, {"catalog": [...]}, список, вложенное дерево с children."""
        if isinstance(data, dict):
            for key in ("pages", "catalog", "categories", "items"):
                if isinstance(data.get(key), list):
                    return cls.from_json_data(data[key])
            if "id" in data and "children" in data:
                flat: list[dict[str, Any]] = []
                cls._flatten(data, flat)
                return cls.from_pages(flat)
            raise ValueError("Не удалось распознать формат дерева разделов")
        if isinstance(data, list):
            if data and isinstance(data[0], dict) and "children" in data[0]:
                flat = []
                for node in data:
                    cls._flatten(node, flat)
                return cls.from_pages(flat)
            return cls.from_pages(data)
        raise ValueError("Не удалось распознать формат дерева разделов")

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "CategoryTree":
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return cls.from_json_data(data)

    def subtree(self, root_id: int, include_root: bool = False) -> "CategoryTree":
        """Поддерево раздела root_id. Пути считаются от него (его собственное название не повторяется).

        include_root=True оставляет и сам раздел (как корень с parent=0), чтобы товар можно было
        отнести к нему, если подраздел не подходит.
        """
        if root_id not in self.nodes:
            raise KeyError(f"Раздел id={root_id} не найден в дереве")
        selected: list[Category] = []
        stack = list(self.children.get(root_id, []))
        while stack:
            cid = stack.pop()
            node = self.nodes[cid]
            parent = 0 if node.parent == root_id and not include_root else node.parent
            selected.append(Category(node.id, parent, node.title_ua, node.title_ru, dict(node.extra)))
            stack.extend(self.children.get(cid, []))
        if include_root:
            root = self.nodes[root_id]
            selected.append(Category(root.id, 0, root.title_ua, root.title_ru, dict(root.extra)))
        return CategoryTree(selected)

    def descendant_ids(self, root_id: int) -> set[int]:
        ids: set[int] = set()
        stack = list(self.children.get(root_id, []))
        while stack:
            cid = stack.pop()
            ids.add(cid)
            stack.extend(self.children.get(cid, []))
        return ids

    def save(self, path: str | os.PathLike[str]) -> None:
        pages = [
            {"id": c.id, "parent": c.parent, "title": {"ua": c.title_ua, "ru": c.title_ru}, **c.extra}
            for c in self.nodes.values()
        ]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pages": pages}, fh, ensure_ascii=False, indent=1)

    # --- запросы ------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, category_id: Any) -> bool:
        try:
            return int(category_id) in self.nodes
        except (TypeError, ValueError):
            return False

    def path(self, category_id: int, lang: str = "ua", sep: str = " / ") -> str:
        parts: list[str] = []
        seen: set[int] = set()
        current = category_id
        while current in self.nodes and current not in seen:
            seen.add(current)
            node = self.nodes[current]
            parts.append(node.title_ua if lang == "ua" else node.title_ru)
            current = node.parent
        return sep.join(reversed(parts))

    def depth(self, category_id: int) -> int:
        return len(self.path(category_id, sep="\x00").split("\x00")) if category_id in self.nodes else 0

    def is_leaf(self, category_id: int) -> bool:
        return category_id in self.nodes and not self.children.get(category_id)

    def leaf_ids(self) -> list[int]:
        return [i for i in self.nodes if self.is_leaf(i)]

    def walk(self, parent: int = 0, level: int = 0):
        """Обход в глубину: (category, уровень)."""
        for cid in self.children.get(parent, []):
            yield self.nodes[cid], level
            yield from self.walk(cid, level + 1)

    def digest(self) -> str:
        """Хэш структуры — входит в ключ кэша, чтобы смена категорий сбрасывала кэш."""
        raw = json.dumps(
            sorted((c.id, c.parent, c.title_ua, c.title_ru) for c in self.nodes.values()), ensure_ascii=False
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def to_prompt_lines(self) -> str:
        """Компактный список для системного промпта: 'id | путь ua | путь ru'."""
        lines = ["id | категорія (ua) | категория (ru)"]
        for cat, _level in self.walk():
            lines.append(f"{cat.id} | {self.path(cat.id, 'ua')} | {self.path(cat.id, 'ru')}")
        return "\n".join(lines)

    def pretty(self) -> str:
        return "\n".join(f"{'  ' * level}[{cat.id}] {cat.title_ua}  /  {cat.title_ru}" for cat, level in self.walk())


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #


class HoroshopClient:
    def __init__(
        self,
        domain: str,
        login: str,
        password: str,
        *,
        transport: Transport | None = None,
        timeout: float = 120.0,
        log: Callable[[str], None] | None = None,
    ):
        self.base_url = self.normalize_domain(domain) + "/api/"
        self.login = login
        self.password = password
        self.timeout = timeout
        self._transport = transport or (lambda url, body, headers: urllib_transport(url, body, headers, timeout))
        self._token: str | None = None
        self._token_time = 0.0
        self.log = log or (lambda msg: None)

    @staticmethod
    def normalize_domain(domain: str) -> str:
        """'soundsgood.com.ua', 'https://soundsgood.com.ua/admin/', 'http://www.site.ua/api' -> 'https://<host>'.

        Оставляет только имя хоста: пути (/admin, /api), параметры и протокол http отбрасываются,
        потому что API Хорошоп доступен по https://<ДОМЕН>/api/.
        """
        raw = domain.strip()
        if "://" in raw:
            raw = raw.split("://", 1)[1]
        host = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip().lower()
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        if not host:
            raise HoroshopError("CONFIG", "Пустой домен сайта (HOROSHOP_DOMAIN)")
        return "https://" + host

    @classmethod
    def from_env(cls, **kwargs: Any) -> "HoroshopClient":
        domain = os.environ.get("HOROSHOP_DOMAIN", "")
        login = os.environ.get("HOROSHOP_LOGIN", "")
        password = os.environ.get("HOROSHOP_PASSWORD", "")
        missing = [n for n, v in (("HOROSHOP_DOMAIN", domain), ("HOROSHOP_LOGIN", login), ("HOROSHOP_PASSWORD", password)) if not v]
        if missing:
            raise HoroshopError("CONFIG", "Не заданы переменные окружения: " + ", ".join(missing))
        return cls(domain, login, password, **kwargs)

    # --- низкий уровень ----------------------------------------------------- #

    def _post(self, function: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{function.strip('/')}/"
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        return self._transport(url, payload, headers)

    def _authenticate(self) -> str:
        result = self._post("auth", {"login": self.login, "password": self.password})
        if result.get("status") != "OK":
            message = (result.get("response") or {}).get("message") if isinstance(result.get("response"), dict) else None
            raise HoroshopError(str(result.get("status")), message or "авторизация не удалась", result)
        self._token = result["response"]["token"]
        self._token_time = time.time()
        self.log("Авторизация в Хорошоп выполнена")
        return self._token

    def token(self) -> str:
        if not self._token or time.time() - self._token_time > TOKEN_TTL_SECONDS - TOKEN_REFRESH_MARGIN:
            return self._authenticate()
        return self._token

    def call(self, function: str, payload: dict[str, Any] | None = None, *, ok_statuses: tuple[str, ...] = ("OK", "EMPTY")) -> dict[str, Any]:
        """Вызывает функцию API с токеном; при UNAUTHORIZED один раз переавторизуется."""
        body = dict(payload or {})
        body["token"] = self.token()
        result = self._post(function, body)
        if result.get("status") in ("UNAUTHORIZED", "AUTHORIZATION_ERROR"):
            body["token"] = self._authenticate()
            result = self._post(function, body)
        status = str(result.get("status"))
        if status not in ok_statuses:
            response = result.get("response")
            message = response.get("message") if isinstance(response, dict) and response.get("message") else json.dumps(response, ensure_ascii=False)[:500]
            raise HoroshopError(status, message, result)
        return result

    def check_connection(self) -> str:
        """Пробует авторизоваться и прочитать корневые разделы. Возвращает текст для пользователя."""
        token = self.token()
        pages = self.export_pages(0)
        return f"Подключение к {self.base_url} работает: токен получен ({len(token)} символов), корневых разделов: {len(pages)}"

    # --- категории ---------------------------------------------------------- #

    def export_pages(self, parent: int = 0) -> list[dict[str, Any]]:
        result = self.call("pages/export", {"parent": parent})
        response = result.get("response") or {}
        return list(response.get("pages") or [])

    def export_category_tree(self) -> CategoryTree:
        """Выгружает всё дерево разделов.

        Документация не уточняет, возвращает ли pages/export всё поддерево или только прямых потомков,
        поэтому: берём ответ для корня; если он плоский (все parent == 0), обходим разделы рекурсивно.
        """
        pages: dict[int, dict[str, Any]] = {}
        queue = [0]
        visited: set[int] = set()
        while queue:
            parent = queue.pop(0)
            if parent in visited:
                continue
            visited.add(parent)
            batch = self.export_pages(parent)
            new_ids = []
            for p in batch:
                pid = int(p["id"])
                if pid not in pages:
                    pages[pid] = p
                    new_ids.append(pid)
            flat = all(int(p.get("parent") or 0) == parent for p in batch)
            if flat:
                queue.extend(new_ids)
        self.log(f"Выгружено разделов: {len(pages)}")
        return CategoryTree.from_pages(pages.values())

    # --- товары ------------------------------------------------------------- #

    def export_products(
        self,
        *,
        offset: int = 0,
        limit: int = EXPORT_PAGE_SIZE,
        included_params: list[str] | None = None,
        excluded_params: list[str] | None = None,
        articles: list[str] | None = None,
        parent_id: int | list[int] | None = None,
        display_in_showcase: int | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"offset": offset, "limit": min(limit, EXPORT_PAGE_SIZE)}
        if included_params:
            payload["includedParams"] = included_params
        if excluded_params:
            payload["excludedParams"] = excluded_params
        if articles:
            payload["article"] = articles
        if parent_id is not None:
            payload["parent"] = {"id": parent_id}
        if display_in_showcase is not None:
            payload["display_in_showcase"] = display_in_showcase
        result = self.call("catalog/export", payload, ok_statuses=("OK", "EMPTY"))
        response = result.get("response")
        if isinstance(response, dict) and "products" in response:
            return list(response["products"] or [])
        return list(result.get("products") or [])

    def export_all_products(self, page_size: int = EXPORT_PAGE_SIZE, **kwargs: Any) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = self.export_products(offset=offset, limit=page_size, **kwargs)
            products.extend(batch)
            self.log(f"Выгружено товаров: {len(products)}")
            if len(batch) < page_size:
                break
            offset += page_size
        return products

    def import_products(self, products: list[dict[str, Any]]) -> dict[str, Any]:
        """catalog/import. Возвращает весь ответ (status OK|WARNING|ERROR, response.log)."""
        return self.call("catalog/import", {"products": products}, ok_statuses=("OK", "WARNING"))


# --------------------------------------------------------------------------- #
# Преобразование товаров Хорошоп в плоские строки
# --------------------------------------------------------------------------- #


def _lang(value: Any, lang: str) -> str:
    if isinstance(value, dict):
        return str(value.get(lang) or "")
    return str(value or "")


def _parent_path(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or value.get("title") or value.get("id") or "")
    if isinstance(value, list):
        return " / ".join(str(v) for v in value)
    return str(value or "").replace("\\", "/")


def flatten_product(product: dict[str, Any]) -> dict[str, Any]:
    """Делает из товара catalog/export плоскую строку для CSV/обработки."""
    parent = product.get("parent")
    row = {
        "article": product.get("article", ""),
        "parent_article": product.get("parent_article", ""),
        "title_ua": _lang(product.get("title"), "ua"),
        "title_ru": _lang(product.get("title"), "ru"),
        "mod_title_ua": _lang(product.get("mod_title"), "ua"),
        "mod_title_ru": _lang(product.get("mod_title"), "ru"),
        "parent": _parent_path(parent),
        "parent_id": parent.get("id", "") if isinstance(parent, dict) else "",
        "brand": product.get("brand", "") or "",
        "price": product.get("price", ""),
        "display_in_showcase": product.get("display_in_showcase", ""),
        "presence": product.get("presence", ""),
    }
    return row


EXPORT_FIELDS = [
    "article", "parent_article", "title", "mod_title", "parent", "brand", "price", "display_in_showcase", "presence",
]
