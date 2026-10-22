"""Точка входа в обработку данных контейнеров."""
import warnings
import traceback

from config import OUTPUT_DIR
from references import load_and_prepare_input
from circles import process_circles_and_enrich
from reports import save_report, save_processed_data

warnings.filterwarnings('ignore', category=FutureWarning)


def main():
    try:
        print("=" * 60)
        print("НАЧАЛО ОБРАБОТКИ ДАННЫХ КТК")
        print(f"Рабочая директория: {OUTPUT_DIR}")
        print("=" * 60)

        df, original_columns, продукты, product2_dict = load_and_prepare_input()
        if df is None or df.empty:
            print("\n❌ Ошибка: Данные пусты или не были загружены.")
            return

        df, circles_df = process_circles_and_enrich(df, original_columns, продукты)

        # Файл 1: отчёт (группировка + свод + детализация).
        report_path = save_report(df, circles_df, str(OUTPUT_DIR))

        # Файл 2: обработанные данные, отрезки и свод по кругам.
        processed_path = save_processed_data(df, str(OUTPUT_DIR), product2_dict, circles_df)

        if report_path and processed_path:
            print("\n✅ Оба файла успешно сформированы!")
        else:
            print("\n⚠️ Обработка завершилась не полностью: один из файлов не был сохранён.")
    except Exception as exc:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА ВЫПОЛНЕНИЯ: {exc}")
        traceback.print_exc()

    input("\nНажмите Enter для выхода из программы...")


if __name__ == "__main__":
    main()
