#!/usr/bin/env python3
"""
app.py — простая оболочка в браузере для всего конвейера.

Запуск:  python app.py   (Windows: двойной клик по start.bat)
Откроется http://127.0.0.1:8765 с вкладками:
  • Товары сайта   — выгрузка из админпанели или с сайта -> выбор поставщика и раздела -> Claude -> review.csv -> на сайт
  • Прайс поставщика — файл поставщика -> названия и разделы -> создание товаров на сайте
  • Разделы        — дерево разделов сайта, обновление с сайта
  • Настройки      — ключ Claude API, домен/логин/пароль Хорошоп, проверка подключений

Сервер слушает только 127.0.0.1 (доступ только с этого компьютера). Файлы, загруженные через страницу,
сохраняются в папку uploads/ рядом со скриптом. Всё остальное делает horoshop_sync.py.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

import product_names as pn  # noqa: E402
from run import ENV_FILE, SETTINGS, read_env_file, write_env_file  # noqa: E402

UPLOAD_DIR = BASE_DIR / "uploads"
WEB_DIR = BASE_DIR / "webui"
CATEGORIES_FILE = "categories.json"
PRODUCTS_FILE = "products.csv"
REVIEW_FILE = "review.csv"
SUPPLIER_REVIEW_FILE = "review_supplier.csv"
HOST, PORT = "127.0.0.1", 8765


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #


def apply_env() -> dict[str, str]:
    values = read_env_file(ENV_FILE)
    for key, value in values.items():
        if value:
            os.environ[key] = value
    return values


def masked_settings() -> list[dict[str, Any]]:
    values = read_env_file(ENV_FILE)
    out = []
    for key, hint, secret in SETTINGS:
        v = values.get(key) or os.environ.get(key, "")
        shown = (v[:6] + "…" + v[-3:] if len(v) > 12 else "*" * len(v)) if (secret and v) else v
        out.append({"key": key, "hint": hint, "secret": secret, "value": shown, "set": bool(v)})
    return out


# --------------------------------------------------------------------------- #
# Фоновая задача с журналом
# --------------------------------------------------------------------------- #


class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.running = False
        self.title = ""
        self.exit_code: int | None = None

    def start(self, title: str, target, *args) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running, self.title, self.exit_code, self.lines = True, title, None, []
        threading.Thread(target=self._run, args=(target, args), daemon=True).start()
        return True

    def _run(self, target, args) -> None:
        writer = _LogWriter(self)
        code = 1
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                code = int(target(*args) or 0)
        except SystemExit as exc:  # argparse и т.п.
            code = int(exc.code or 0) if isinstance(exc.code, int) else 1
        except Exception:  # noqa: BLE001
            writer.write("Ошибка:\n" + traceback.format_exc())
            code = 1
        finally:
            writer.flush()
            with self.lock:
                self.running, self.exit_code = False, code
                self.lines.append(f"[завершено, код {code}]")

    def append(self, text: str) -> None:
        with self.lock:
            self.lines.extend(text.splitlines())
            if len(self.lines) > 5000:
                del self.lines[: len(self.lines) - 5000]

    def snapshot(self, since: int) -> dict[str, Any]:
        with self.lock:
            return {"running": self.running, "title": self.title, "exit_code": self.exit_code,
                    "lines": self.lines[since:], "total": len(self.lines)}


class _LogWriter(io.TextIOBase):
    def __init__(self, job: Job) -> None:
        self.job = job
        self.buf = ""

    def write(self, s: str) -> int:  # type: ignore[override]
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.job.append(line)
        return len(s)

    def flush(self) -> None:
        if self.buf:
            self.job.append(self.buf)
            self.buf = ""


JOB = Job()


def run_sync(argv: list[str]) -> int:
    import horoshop_sync

    apply_env()
    print("> horoshop_sync.py " + " ".join(argv))
    return horoshop_sync.main(argv)


# --------------------------------------------------------------------------- #
# Анализ файлов
# --------------------------------------------------------------------------- #


def analyze_products_file(path: Path) -> dict[str, Any]:
    import horoshop_sync

    rows = horoshop_sync.load_product_rows(path)
    info: dict[str, Any] = {"path": str(path), "rows": len(rows), "columns": list(rows[0].keys()) if rows else [],
                            "admin_export": bool(rows and "supplier" in rows[0] and "title_ua" in rows[0])}
    if rows and "supplier" in rows[0]:
        counts = Counter((str(r.get("supplier") or "").strip() or "(пусто)") for r in rows)
        visible = Counter((str(r.get("supplier") or "").strip() or "(пусто)") for r in rows
                          if str(r.get("display_in_showcase", "1")) in ("1", "Да"))
        info["suppliers"] = [{"name": n, "count": c, "visible": visible.get(n, 0)} for n, c in counts.most_common()]
    return info


def analyze_supplier_file(path: Path) -> dict[str, Any]:
    rows = pn.read_products(path)
    sample = rows[0] if rows else {}
    cols = {
        "name": pn.pick_column(sample, None, pn.NAME_COLUMNS),
        "article": pn.pick_column(sample, None, pn.ARTICLE_COLUMNS),
        "brand": pn.pick_column(sample, None, pn.BRAND_COLUMNS),
        "price": pn.pick_column(sample, None, pn.PRICE_COLUMNS),
        "category": pn.pick_column(sample, None, pn.CATEGORY_COLUMNS),
    }
    return {"path": str(path), "rows": len(rows), "columns": list(sample.keys()), "detected": cols,
            "preview": [{k: ("" if v is None else str(v))[:80] for k, v in r.items()} for r in rows[:5]]}


def categories_list() -> list[dict[str, Any]]:
    from horoshop_client import CategoryTree

    if not Path(CATEGORIES_FILE).exists():
        return []
    tree = CategoryTree.load(CATEGORIES_FILE)
    out = []
    for cat, level in tree.walk():
        path = tree.path(cat.id, "ua")
        parts = path.split(" / ")
        # Короткий путь без служебных верхних уровней (главная страница сайта, «Каталог ...»)
        short = " / ".join(parts[2:]) if level >= 2 else path
        out.append({"id": cat.id, "level": level, "title": cat.title_ua, "path": path, "short": short,
                    "leaf": tree.is_leaf(cat.id), "on_site": cat.extra.get("on_site")})
    return out


def review_summary(path: Path, limit: int = 200) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    rows = pn.read_products(path)
    cols = [c for c in ("article", "source_name", "title_ua", "name_uk", "name_ru", "category_path", "category_changed",
                        "supplier", "price", "comment", "apply") if rows and c in rows[0]]
    return {
        "exists": True, "rows": len(rows), "columns": cols,
        "unassigned": sum(1 for r in rows if not str(r.get("category_id") or "").strip() or str(r.get("category_id")) == "0"),
        "to_apply": sum(1 for r in rows if str(r.get("apply", "")).strip().lower() == "yes"),
        "preview": [{c: str(r.get(c, ""))[:120] for c in cols} for r in rows[:limit]],
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "ProductsUI/1.0"

    def log_message(self, *_args: Any) -> None:  # тихий сервер
        return

    # --- helpers --------------------------------------------------------- #

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str, download: bool = False) -> None:
        if not path.exists():
            self._json({"error": "файл не найден"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    # --- GET ------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._json({
                    "settings": masked_settings(),
                    "files": {name: Path(name).exists() for name in (CATEGORIES_FILE, PRODUCTS_FILE, REVIEW_FILE, SUPPLIER_REVIEW_FILE)},
                    "job": JOB.snapshot(0) | {"lines": []},
                    "base_dir": str(BASE_DIR),
                })
            elif url.path == "/api/job":
                self._json(JOB.snapshot(int(q.get("since", ["0"])[0])))
            elif url.path == "/api/categories":
                self._json(categories_list())
            elif url.path == "/api/review":
                self._json(review_summary(BASE_DIR / Path(q.get("file", [REVIEW_FILE])[0]).name))
            elif url.path == "/download":
                name = Path(q.get("file", [""])[0]).name
                self._file(BASE_DIR / name, "text/csv; charset=utf-8", download=True)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    # --- POST ------------------------------------------------------------ #

    def do_POST(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/api/settings":
                data = self._read_json()
                values = read_env_file(ENV_FILE)
                for key, _hint, _secret in SETTINGS:
                    v = str(data.get(key, "")).strip().strip("\"'")
                    if v:
                        values[key] = v
                write_env_file(ENV_FILE, values)
                apply_env()
                self._json({"ok": True, "settings": masked_settings()})
            elif url.path == "/api/check/claude":
                self._json(check_claude())
            elif url.path == "/api/check/horoshop":
                self._json(check_horoshop())
            elif url.path == "/api/upload":
                self._json(self._handle_upload())
            elif url.path == "/api/run":
                data = self._read_json()
                argv, title = build_argv(data)
                started = JOB.start(title, run_sync, argv)
                self._json({"ok": started, "argv": argv, "error": None if started else "уже выполняется другая задача"})
            elif url.path == "/api/review/apply_flags":
                data = self._read_json()
                self._json(update_apply_flags(Path(Path(data.get("file", REVIEW_FILE)).name), data.get("articles", []), data.get("value", "yes")))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc), "trace": traceback.format_exc()}, 500)

    def _handle_upload(self) -> dict[str, Any]:
        """Принимает multipart/form-data с полями kind (products|supplier) и file."""
        import email
        import email.policy

        ctype = self.headers.get("Content-Type", "")
        body = self._read_body()
        msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body, policy=email.policy.HTTP)
        kind, filename, payload = "products", "", b""
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "kind":
                kind = part.get_content().strip() if isinstance(part.get_content(), str) else part.get_payload(decode=True).decode()
            elif name == "file":
                filename = Path(part.get_filename() or "upload.bin").name
                payload = part.get_payload(decode=True) or b""
        if not filename or not payload:
            return {"error": "файл не получен"}
        UPLOAD_DIR.mkdir(exist_ok=True)
        target = UPLOAD_DIR / filename
        target.write_bytes(payload)
        info = analyze_products_file(target) if kind == "products" else analyze_supplier_file(target)
        info["kind"] = kind
        return info


def check_claude() -> dict[str, Any]:
    apply_env()
    try:
        client = pn.make_client()
        model = client.models.retrieve(pn.DEFAULT_MODEL)
        return {"ok": True, "message": f"Ключ принят, модель доступна: {model.display_name}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": pn.describe_api_error(exc)}


def check_horoshop() -> dict[str, Any]:
    from horoshop_client import HoroshopClient, HoroshopError

    apply_env()
    try:
        client = HoroshopClient.from_env()
        return {"ok": True, "message": client.check_connection()}
    except HoroshopError as exc:
        hint = " Проверьте логин и пароль API-администратора." if exc.status == "ERROR" else ""
        return {"ok": False, "message": f"{exc}{hint}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def build_argv(data: dict[str, Any]) -> tuple[list[str], str]:
    """Собирает аргументы horoshop_sync из формы."""
    cmd = data.get("command")
    p = data.get("params") or {}

    def common(argv: list[str]) -> list[str]:
        if p.get("root_id"):
            argv += ["--root-id", str(int(p["root_id"]))]
        if p.get("limit"):
            argv += ["--limit", str(int(p["limit"]))]
        if p.get("web_check"):
            argv.append("--web-check")
        if p.get("effort") in ("low", "medium", "high", "xhigh", "max"):
            argv += ["--effort", p["effort"]]
        if p.get("offline"):
            argv.append("--offline")
        return argv

    if cmd == "process":
        argv = ["process", "--categories", CATEGORIES_FILE, "--out", REVIEW_FILE]
        if p.get("input"):
            argv += ["--in", str(p["input"])]
        if p.get("supplier"):
            argv += ["--supplier", str(p["supplier"])]
        if p.get("only_visible"):
            argv.append("--only-visible")
        return common(argv), "Обработка товаров сайта"
    if cmd == "supplier":
        argv = ["supplier", "--in", str(p.get("input", "")), "--categories", CATEGORIES_FILE, "--out", SUPPLIER_REVIEW_FILE]
        for key, flag in (("name_col", "--name-col"), ("article_col", "--article-col"), ("brand_col", "--brand-col"),
                          ("price_col", "--price-col"), ("category_col", "--category-col"), ("sheet", "--sheet")):
            if p.get(key):
                argv += [flag, str(p[key])]
        if p.get("supplier"):
            argv += ["--supplier", str(p["supplier"])]
        return common(argv), "Обработка прайса поставщика"
    if cmd == "apply":
        argv = ["apply", "--in", str(Path(p.get("file", REVIEW_FILE)).name)]
        if p.get("create"):
            argv.append("--create")
        argv.append("--yes" if p.get("confirm") else "--dry-run")
        return argv, "Загрузка на сайт" if p.get("confirm") else "Предпросмотр загрузки"
    if cmd == "categories":
        return ["categories", "--out", CATEGORIES_FILE], "Выгрузка разделов с сайта"
    if cmd == "export":
        return ["export", "--out", PRODUCTS_FILE], "Выгрузка товаров с сайта"
    raise ValueError(f"неизвестная команда: {cmd}")


def update_apply_flags(path: Path, articles: list[str], value: str) -> dict[str, Any]:
    rows = pn.read_products(path)
    wanted = {str(a) for a in articles}
    changed = 0
    for r in rows:
        if str(r.get("article", "")) in wanted:
            r["apply"] = value
            changed += 1
    pn.write_products(rows, path)
    return {"ok": True, "changed": changed}


def main() -> int:
    apply_env()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Оболочка запущена: {url}  (закройте это окно, чтобы остановить)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
