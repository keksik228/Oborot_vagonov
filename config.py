"""Общие пути проекта. Поиск учитывает варианты названий папок и справочников."""
from pathlib import Path
import re
import unicodedata

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR


def _key(value):
    value = unicodedata.normalize("NFKC", str(value)).replace("ё", "е").casefold()
    return "".join(ch for ch in value if ch.isalnum())


def _find_dir(names, fallback):
    wanted = {_key(name) for name in names}
    for child in BASE_DIR.iterdir():
        if child.is_dir() and _key(child.name) in wanted:
            return child
    return fallback


def _resolve_file(folder, names, fallback_folder=None):
    """Ищет файл сначала в папке назначения, затем в проекте по нормализованному имени."""
    names = list(names)
    candidate_folders = []
    for candidate in (folder, fallback_folder, BASE_DIR):
        if candidate is not None and candidate.exists() and candidate.is_dir() and candidate not in candidate_folders:
            candidate_folders.append(candidate)

    # Сначала буквальное совпадение имени — быстрее и наиболее предсказуемо.
    for candidate_folder in candidate_folders:
        for name in names:
            candidate = candidate_folder / name
            if candidate.is_file():
                return candidate

    wanted = {_key(Path(name).stem) for name in names}
    # Потом поиск по именам без учёта регистра, пробелов, скобок и подчёркиваний.
    for candidate_folder in candidate_folders:
        try:
            for candidate in candidate_folder.rglob("*.xlsx"):
                if _key(candidate.stem) in wanted:
                    return candidate
        except (OSError, PermissionError):
            continue
    return folder / names[0]


SOURCE_DIR = _find_dir(("Источник", "Источники", "Source", "Sources"), BASE_DIR / "Источник")
REFERENCES_DIR = _find_dir(("Справочник", "Справочники", "Reference", "References"), BASE_DIR / "Справочник")

SOURCE_FILE = _resolve_file(
    SOURCE_DIR,
    ("выгрузка Истории КТК.xlsx", "выгрузка_Истории_КТК.xlsx", "История КТК.xlsx"),
    fallback_folder=BASE_DIR,
)
PLACES_FILE = _resolve_file(
    REFERENCES_DIR,
    ("Справочник_место.xlsx", "Справочник_мест.xlsx", "Справочник мест.xlsx", "Справочник_места.xlsx"),
    fallback_folder=BASE_DIR,
)
PRODUCTS_FILE = _resolve_file(REFERENCES_DIR, ("Продукты.xlsx", "Продукт.xlsx"), fallback_folder=BASE_DIR)
SEGMENTS_FILE = _resolve_file(REFERENCES_DIR, ("Отрезки.xlsx", "Справочник_отрезки.xlsx"), fallback_folder=BASE_DIR)
SCHEMES_FILE = _resolve_file(REFERENCES_DIR, ("Схемы.xlsx", "Схема.xlsx"), fallback_folder=BASE_DIR)

MONTH_NAMES = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
