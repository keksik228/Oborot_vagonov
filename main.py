
"""Точка входа в обработку данных контейнеров."""

import hashlib
import importlib.metadata
import sys
import warnings
import traceback
from pathlib import Path

import pandas as pd
import numpy as np

import config
import references
import circles
import segments
import reports

from config import OUTPUT_DIR
from references import load_and_prepare_input
from circles import process_circles_and_enrich
from reports import save_processed_data

warnings.filterwarnings("ignore", category=FutureWarning)

import pandas as pd

if pd.__version__ != "2.3.3":
    raise RuntimeError(
        f"Неподдерживаемая версия Pandas: {pd.__version__}. "
        "Для корректной работы установите pandas==2.3.3"
    )

def main():

    try:

        print("=" * 60)
        print("НАЧАЛО ОБРАБОТКИ ДАННЫХ КТК")
        print(f"Рабочая директория: {OUTPUT_DIR}")
        print("=" * 60)

        # 1. Загрузка исходных данных и справочников
        df, original_columns, продукты, product2_dict = (
            load_and_prepare_input()
        )

        if df is None or df.empty:
            print("\n❌ Ошибка: данные пусты или не были загружены.")
            return

        print(f"\nИсходных строк для обработки: {len(df):,}")

        # 2. Поиск кругов и обогащение данных
        df, circles_df = process_circles_and_enrich(
            df,
            original_columns,
            продукты
        )

        if df is None or df.empty:
            print("\n❌ Ошибка: после расчёта данные пусты.")
            return

        print("\n" + "=" * 60)
        print("ОБРАБОТКА ЗАВЕРШЕНА")
        print("=" * 60)

        print(f"Обработано строк: {len(df):,}")

        if circles_df is not None:
            print(f"Найдено завершённых кругов: {len(circles_df):,}")

        if "Номер круга" in df.columns:
            print(
                "Пронумеровано строк: "
                f"{df['Номер круга'].notna().sum():,}"
            )

        # 3. Формирование только файла «Обработанные данные»
        # Отдельный файл «ОТЧЕТ_оборот_*.xlsx» не создаём.

        print("\nСохранение обработанных данных...")

        processed_path = save_processed_data(
            df,
            str(OUTPUT_DIR),
            product2_dict,
            circles_df
        )

        if processed_path:
            print("\n✅ Файл обработанных данных успешно сформирован!")
            print(f"Файл: {processed_path}")
        else:
            print(
                "\n❌ Не удалось сформировать "
                "файл обработанных данных."
            )

    except Exception as exc:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА ВЫПОЛНЕНИЯ: {exc}")
        traceback.print_exc()

    try:
        input("\nНажмите Enter для выхода из программы...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
