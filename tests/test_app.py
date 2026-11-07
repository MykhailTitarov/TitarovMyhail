"""Тесты оболочки app.py без запуска сервера: сборка аргументов, анализ файлов, сводка review."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
import product_names as pn  # noqa: E402


def test_build_argv_process_and_supplier():
    argv, title = app.build_argv({"command": "process", "params": {"input": "uploads/x.xlsx", "supplier": "Thomann", "root_id": "1256",
                                                                   "limit": "30", "only_visible": True, "web_check": True, "effort": "low"}})
    assert argv[:1] == ["process"] and "--in" in argv and "--supplier" in argv and "--root-id" in argv and "--only-visible" in argv
    assert argv[argv.index("--limit") + 1] == "30" and "--web-check" in argv and argv[argv.index("--effort") + 1] == "low"
    assert "Обработка" in title

    argv, _ = app.build_argv({"command": "supplier", "params": {"input": "uploads/p.xlsx", "name_col": "Найменування", "offline": True}})
    assert argv[argv.index("--name-col") + 1] == "Найменування" and "--offline" in argv and "--web-check" not in argv


def test_build_argv_apply_requires_confirm_for_yes():
    argv, _ = app.build_argv({"command": "apply", "params": {"file": "../../etc/passwd", "create": True}})
    assert argv == ["apply", "--in", "passwd", "--create", "--dry-run"]   # путь обрезан до имени, без --yes
    argv, _ = app.build_argv({"command": "apply", "params": {"file": "review.csv", "confirm": True}})
    assert argv[-1] == "--yes"
    with pytest.raises(ValueError):
        app.build_argv({"command": "rm -rf", "params": {}})


def test_analyze_supplier_and_review(tmp_path):
    sample = Path(__file__).resolve().parents[1] / "examples" / "supplier_price_sample.xlsx"
    info = app.analyze_supplier_file(sample)
    assert info["rows"] == 13 and info["detected"]["name"] == "Найменування" and info["detected"]["price"] == "Ціна"
    assert len(info["preview"]) == 5

    review = tmp_path / "review.csv"
    pn.write_products([{"article": "A1", "name_uk": "x", "name_ru": "y", "category_id": 12, "category_path": "a / b", "apply": "yes"},
                       {"article": "A2", "name_uk": "x", "name_ru": "y", "category_id": 0, "category_path": "", "apply": ""}], review)
    s = app.review_summary(review)
    assert s["rows"] == 2 and s["unassigned"] == 1 and s["to_apply"] == 1 and "apply" in s["columns"]
    assert app.update_apply_flags(review, ["A2"], "yes")["changed"] == 1
    assert app.review_summary(review)["to_apply"] == 2
    assert app.review_summary(tmp_path / "missing.csv") == {"exists": False}


def test_masked_settings_hides_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-1234567890abcdef")
    items = {s["key"]: s for s in app.masked_settings()}
    assert items["ANTHROPIC_API_KEY"]["set"] and "1234567890abcdef" not in items["ANTHROPIC_API_KEY"]["value"]
