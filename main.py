"""Точка входа в обработку данных контейнеров."""

import warnings
import traceback

from config import OUTPUT_DIR
from references import load_and_prepare_input
from circles import process_circles_and_enrich
from reports import save_processed_data

warnings.filterwarnings("ignore", category=FutureWarning)


def main():
    try:
        print("=" * 60)
        print("НАЧАЛО ОБРАБОТКИ ДАННЫХ КТК")
        print(f"Рабочая директория: {OUTPUT_DIR}")
        print("=" * 60)

        # 1. Загружаем исходные данные и справочники
        df, original_columns, продукты, product2_dict = (
            load_and_prepare_input()
        )

        if df is None or df.empty:
            print("\n❌ Ошибка: данные пусты или не были загружены.")
            return

        # 2. Находим круги и обогащаем данные
        df, circles_df = process_circles_and_enrich(
            df,
            original_columns,
            продукты,
        )

        # 3. Формируем только файл «Обработанные данные»
        # Отдельный файл «ОТЧЕТ_оборот_*.xlsx» не создаём.
        processed_path = save_processed_data(
            df,
            str(OUTPUT_DIR),
            product2_dict,
            circles_df,
        )

        if processed_path:
            print("\n✅ Обработанные данные успешно сохранены!")
            print(f"Файл: {processed_path}")
        else:
            print(
                "\n❌ Не удалось сохранить файл "
                "обработанных данных."
            )

    except Exception as exc:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {exc}")
        traceback.print_exc()

    input("\nНажмите Enter для выхода из программы...")


if __name__ == "__main__":
    main()