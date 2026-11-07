# Памятка для следующей сессии (состояние на 2026-11-07)

Проект: названия товаров UA/RU, веб-проверка написания и раскладка по разделам сайта soundsgood.com.ua (Хорошоп).
Ветка: `claude/product-sorting-script-api-d0v6an`. Документация: `README_product_names.md`.

## Что уже есть

- `product_names.py` — названия/категории через Claude API (`--web-check`, `--categories`, сортировка кириллицы).
- `horoshop_client.py` — API Хорошоп (auth, pages/export, catalog/export, catalog/import), `CategoryTree`.
- `horoshop_sync.py` — команды `categories`, `export`, `suppliers`, `process`, `supplier`, `apply`.
- `run.py` + `start.bat` — меню для запуска на Windows.
- `categories.json` — полное дерево сайта (1586 разделов, корень каталога id 97), сверено с публичным сайтом
  2026-11-06: расхождений нет, 122 раздела из API не опубликованы (отчёт в `reports/`).

## Доступы (переменные среды «SoundsGood», заданы пользователем)

`ANTHROPIC_API_KEY`, `HOROSHOP_DOMAIN` (soundsgood.com.ua), `HOROSHOP_LOGIN`, `HOROSHOP_PASSWORD`.
Проверка: `python horoshop_sync.py categories --out /tmp/x.json` (должно выгрузить дерево).

## Каталог товаров

Выгрузка из админпанели Хорошоп (xlsx, 56 МБ, 20 721 товар, 80 колонок, есть колонка «Поставщик»):
https://docs.google.com/spreadsheets/d/1uecWRupiJnlM5yMExOhHMiwjaG31iPv6/edit?usp=sharing
Прямое скачивание: `curl -L -o catalog.xlsx "https://drive.google.com/uc?export=download&id=1uecWRupiJnlM5yMExOhHMiwjaG31iPv6&confirm=t"`
(хосты docs.google.com, drive.google.com, drive.usercontent.google.com должны быть разрешены в среде).
Скрипт распознаёт этот формат сам: `python horoshop_sync.py suppliers --in catalog.xlsx`.

Поставщики: m.jam.ua 9356, Acropolis 4849, Komora 1848, без поставщика 1495, Thomann 1274, Karma 1148, ещё 19 мелких.
Раздел «Клавішні інструменти» (id 1256): 909 товаров (m.jam.ua 555, без поставщика 235, Acropolis 127, Komora 81, Thomann 18, Prodj 13).
Особенности: у 3379 товаров пустое название UA (берётся RU); 645 скрыты; у 29 товаров раздел, которого нет в дереве
(4 подраздела тростей для тенор-саксофона D'Addario/YAMAHA). Разделы с 404 на сайте: /dysklaviry/ (id 2237), /spotlaity/.

## План

1. Пробный прогон: `python horoshop_sync.py process --in catalog.xlsx --supplier <имя> --only-visible --root-id 1256 --limit 30 --web-check --out review.csv`
2. Показать пользователю `review.csv`, получить правки/подтверждение.
3. `python horoshop_sync.py apply --in review.csv --dry-run`, затем с `--yes` только после явного «да» пользователя.
4. Дальше по поставщикам/разделам, которые назовёт пользователь.

Пользователь пишет по-русски; пароль и ключ в чат не просить.
