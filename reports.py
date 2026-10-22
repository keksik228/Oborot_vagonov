"""Формирование отчёта и книги с обработанными данными."""
import os
from datetime import datetime
import pandas as pd

from config import MONTH_NAMES, SEGMENTS_FILE
from references import (
    load_excel_safe,
    clean_string_columns,
    build_segment_dict,
    map_segment_schemes,
    _segment_reference_columns,
    _missing_segment_reference_columns,
)
from circles import _build_circle_date_range
from segments import build_circle_event_schema

def save_report(df, circles_df, output_folder):
    """
    Файл 1: ОТЧЕТ — 3 листа: группировка, СВОД, детализация
    Структура точно соответствует образцу ОТЧЕТ_оборот_новый.xlsx
    """
    os.makedirs(output_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = os.path.join(output_folder, f"ОТЧЕТ_оборот_{timestamp}.xlsx")

    print(f"\n  Сохранение отчёта...")

    with pd.ExcelWriter(report_file, engine='openpyxl') as writer:

        # ── Лист 1: группировка ──────────────────────────────────────────────
        grouping_df = circles_df[[
            'Номер КТК', 'Основной маршрут', 'Продукт', 'Номер круга',
            'Продолжительность (дней)', 'Начало', 'Окончание', 'Вид расчета'
        ]].copy()
        grouping_df.columns = [
            'Номер КТК', 'Основной маршрут', 'Продукт', 'Номер круга',
            'Дней', 'Дата первой точки', 'Дата последней точки', 'Расчет точек'
        ]
        grouping_df.to_excel(writer, sheet_name='группировка', index=False)
        print(f"    ✓ Лист 'группировка' ({len(grouping_df):,} кругов)")

        # ── Лист 2: СВОД ─────────────────────────────────────────────────────
        ws_svod = writer.book.create_sheet('СВОД')

        # Подготовка данных для сводки по кварталам/месяцам
        cdf = circles_df.copy()
        cdf['_dt'] = pd.to_datetime(cdf['Начало'], format='%d.%m.%Y', errors='coerce')
        cdf['_год'] = cdf['_dt'].dt.year
        cdf['_квартал'] = 'Q' + cdf['_dt'].dt.quarter.astype(str)
        cdf['_месяц_num'] = cdf['_dt'].dt.month
        cdf['_месяц'] = cdf['_месяц_num'].map(MONTH_NAMES)

        total_ktk = cdf['Номер КТК'].nunique()
        total_avg = round(cdf['Продолжительность (дней)'].mean(), 2)

        # Сводка по месяцам
        pivot = cdf.groupby(['_год', '_квартал', '_месяц_num', '_месяц']).agg(
            оборот=('Продолжительность (дней)', 'mean'),
            ктк=('Номер КТК', 'count')
        ).reset_index().sort_values(['_год', '_месяц_num'])

        # Заполняем лист
        ws_svod.append([])
        ws_svod.append([])
        ws_svod.append([None, 'Расчет оборота ктк на КРУГ по продуктам (направлениям)'])
        ws_svod.append([None, 'по прибытию в Китай'])
        ws_svod.append([])

        # Получаем диапазон дат
        min_date = cdf['_dt'].min()
        max_date = cdf['_dt'].max()
        date_range = f"Выборка по данным системы DX с {min_date.strftime('%d.%m.%Y')} по {max_date.strftime('%d.%m.%Y')}"
        ws_svod.append([None, date_range])
        ws_svod.append([None, 'Учитываются КТК, совершившие полный круг, без учета СПОТа: ', total_ktk])
        ws_svod.append([None, 'Общий средний оборот составил, сут.: ', total_avg])
        ws_svod.append([None, ' * в расчеты не учитываются ктк с выявленными ошибками в дислокации'])
        ws_svod.append([])

        # Заголовок таблицы
        ws_svod.append(['Год', 'Квартал', 'Месяц', 'Оборот, сут', ' КТК'])

        # Данные по месяцам
        for _, row in pivot.iterrows():
            ws_svod.append([
                int(row['_год']),
                row['_квартал'],
                row['_месяц'],
                round(row['оборот'], 10),
                int(row['ктк'])
            ])

        print(f"    ✓ Лист 'СВОД' (средний оборот: {total_avg} сут., КТК: {total_ktk})")

        # ── Лист 3: детализация ──────────────────────────────────────────────
        # Колонки как в образце
        det_cols = [
            'Номер КТК', 'Маршрут следования', '№ события', 'Дата события',
            'Первая точка', 'Последняя точка', 'Основной маршрут',
            'Отрезок_между_флагами', 'Проверка круга', 'Flag_point', 'Локация',
            'Место события', 'Источник', 'Учетное направление', 'Тип движения',
            'Продукт', 'Собственность КТК', 'Состояние КТК', 'Номер круга',
            'Вид расчета', 'Дней между событиями'
        ]
        existing_cols = [c for c in det_cols if c in df.columns]
        df_det = df[existing_cols].copy()

        # Строка-заголовок раздела (как в образце: первая строка = "Детализация")
        # Пишем через openpyxl вручную: строка 1 = "Детализация", строка 2 = колонки
        ws_det = writer.book.create_sheet('детализация')
        ws_det.append(['Детализация'])
        ws_det.append(existing_cols)

        # Данные
        for row in df_det.itertuples(index=False):
            ws_det.append(list(row))

        print(f"    ✓ Лист 'детализация' ({len(df_det):,} строк)")

    print(f"    ✓ Файл отчёта: {os.path.basename(report_file)}")
    return report_file

def save_processed_data(df, output_folder, product2_dict=None, circles_df=None):
    """
    Файл 2: Обработанные данные
    Лист 1: Обработанные данные
    Лист 2: Свод по незавершённым кругам
    Лист 3: Свод по кругам (только завершённые круги)
    """
    os.makedirs(output_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    processed_file = os.path.join(output_folder, f"обработанные_данные_{timestamp}.xlsx")

    print(f"\n  Сохранение обработанных данных...")
    print(f"    Колонки df: {df.columns.tolist()[:6]}...")
    print(f"    'Номер КТК' в df: {'Номер КТК' in df.columns}")

    # Отдельный лист «Отрезки маршрута» больше не формируется. Колонки
    # переходов в «Обработанных данных» строятся ниже напрямую из событий.
    print(f"    Строк с номером круга: {df['Номер круга'].notna().sum():,}")

    # Добавляем колонку "Следующий отрезок" в df: переход "Локация текущей строки -> Локация следующей строки",
    # по ВСЕМ строкам одного КТК подряд (не только внутри круга), с повторами, если локация не поменялась.
    # Для последней строки КТК (следующей строки уже нет) - "нет следующего назначения".
    df = df.copy()
    next_location = df.groupby('Номер КТК')['Локация'].shift(-1)
    prev_location = df.groupby('Номер КТК')['Локация'].shift(1)
    next_move_type = df.groupby('Номер КТК')['Тип движения'].shift(-1)
    prev_move_type = df.groupby('Номер КТК')['Тип движения'].shift(1)

    has_next = next_location.notna()
    has_prev = prev_location.notna()
    has_current = df['Локация'].notna()

    df['Предыдущая локация'] = prev_location
    df['Следующая локация'] = next_location
    df['Предыдущий Тип движения'] = prev_move_type
    df['Следующий Тип движения'] = next_move_type

    df['Отрезок'] = None
    df.loc[has_next & has_current, 'Отрезок'] = (
        df.loc[has_next & has_current, 'Локация'].astype(str)
        + ' → '
        + next_location.loc[has_next & has_current].astype(str)
    )
    df.loc[~has_next, 'Отрезок'] = 'нет следующего назначения'

    df['Предыдущий отрезок'] = None
    df.loc[has_prev & has_current, 'Предыдущий отрезок'] = (
        prev_location.loc[has_prev & has_current].astype(str)
        + ' → '
        + df.loc[has_prev & has_current, 'Локация'].astype(str)
    )
    df.loc[~has_prev, 'Предыдущий отрезок'] = 'нет предыдущего назначения'

    отрезки = load_excel_safe(SEGMENTS_FILE)
    if отрезки is None:
        print("    КРИТИЧЕСКАЯ ОШИБКА: не удалось загрузить справочник Отрезки.xlsx")
        return None

    missing_segment_cols = _missing_segment_reference_columns(отрезки)
    if missing_segment_cols:
        print("    КРИТИЧЕСКАЯ ОШИБКА: в Отрезки.xlsx отсутствуют колонки: "
              + ', '.join(missing_segment_cols))
        print("    Ожидаются параметры: Следующий отрезок, Перегруз, Учетное направление, Отрезок схемы.")
        return None

    segment_col, overload_col, direction_col, scheme_col = _segment_reference_columns(отрезки)
    отрезки = clean_string_columns(
        отрезки,
        [segment_col, overload_col, direction_col, scheme_col]
    )
    segment_dict = build_segment_dict(отрезки)

    overload_series = df['Перегруз'] if 'Перегруз' in df.columns else pd.Series(None, index=df.index)
    direction_series = df['Учетное направление'] if 'Учетное направление' in df.columns else pd.Series(None, index=df.index)
    df['Отрезок схемы'] = map_segment_schemes(
        df['Отрезок'], overload_series, direction_series, segment_dict
    )
    matched_count = int(df['Отрезок схемы'].notna().sum())
    print(f"    'Отрезок схемы' сопоставлен для {matched_count:,} строк; без совпадения осталось {len(df) - matched_count:,}.")

    # Точные даты границ берём из результатов поиска кругов. Нельзя рассчитывать
    # максимум только по строкам с одним Номером круга: конечное прибытие в Китай
    # одновременно является началом следующего круга и при разметке может получить
    # номер уже следующего круга.
    circle_date_range = _build_circle_date_range(circles_df)
    # Если круг не завершён и его нет в circles_df, не выводим max даты событий
    # в качестве даты возврата в Китай: в таком случае конечная точка остаётся пустой.

    # Свод по кругам: только завершённые круги, с восстановлением конечной точки в Китае.
    print("    Формирую лист 'Свод по кругам'...", flush=True)
    circle_event_schema_df = build_circle_event_schema(
        df, segment_dict, circle_date_range, product2_dict, circles_df,
        include_incomplete=False,
    )
    print(f"    'Свод по кругам' подготовлен: {len(circle_event_schema_df):,} строк.", flush=True)

    # Отдельный свод незавершённых кругов: последняя точка — последнее известное
    # событие в выгрузке, «Время круга» не рассчитывается и не выводится.
    print("    Формирую лист 'Свод по незавершенным кругам'...", flush=True)
    incomplete_circle_schema_df = build_circle_event_schema(
        df, segment_dict, circle_date_range, product2_dict, circles_df,
        include_incomplete=True,
    )
    incomplete_columns = [
        'Номер КТК', 'Номер круга', 'Основной маршрут',
        'Первая точка круга', 'Последняя точка круга',
        'Схема участка', '№ отрезка', 'Отрезок', 'Тип движения',
        'дата начала операции', 'дата окончания операции', 'Длительность операции',
        'Схема движения по отрезкам', 'Продукт 2',
    ]
    if incomplete_circle_schema_df.empty:
        incomplete_circle_schema_df = pd.DataFrame(columns=incomplete_columns)
    print(
        f"    'Свод по незавершенным кругам' подготовлен: "
        f"{len(incomplete_circle_schema_df):,} строк.", flush=True
    )

    # Переименовываем колонки для итогового вывода
    df = df.rename(columns={
        'Отрезок': 'Следующий отрезок',
        'Локация': 'Текущая локация',
    })

    # Порядок колонок как в образце
    desired_order = [
        'Номер КТК', 'Собственник', 'Типоразмер', 'Дата события',
        'Место события', 'Место назначения', 'Тип движения', 'Состояние КТК',
        'Грузоотправитель', 'Грузополучатель', 'Вид транспорта', 'Источник',
        'Кластер события', 'Кластер назначения', 'Собственность КТК',
        'Учетное направление', 'Перегруз',
        '№ события', 'Дней между событиями', 'Маршрут следования',
        'Предыдущая локация', 'Текущая локация', 'Следующая локация',
        'Предыдущий Тип движения', 'Следующий Тип движения',
        'Flag_point', 'Номер круга', 'Отрезок_между_флагами', 'Вид расчета',
        'Основной маршрут', 'Предыдущий отрезок', 'Следующий отрезок', 'Отрезок схемы', 'Операция',
        'Первая точка', 'Последняя точка', 'Продукт',
        'Проверка круга', 'Основной маршрут (общий)', 'Количество кругов'
    ]
    final_cols = [c for c in desired_order if c in df.columns]
    extra = [c for c in df.columns if c not in final_cols]
    final_cols = final_cols + extra

    df_out = df[final_cols].copy()

    print("    Подготовка завершена; начинаю запись Excel. На листе 'Обработанные данные' сотни тысяч строк, запись может занять несколько минут.", flush=True)
    # Для большого файла используем XlsxWriter: запись сотен тысяч строк
    # обычно заметно быстрее, чем openpyxl. Не включаем constant_memory,
    # поскольку этот режим несовместим с порядком записи ячеек pandas.to_excel.
    with pd.ExcelWriter(
        processed_file,
        engine='xlsxwriter',
        engine_kwargs={
            'options': {
                'strings_to_formulas': False,
                'strings_to_urls': False,
            }
        },
    ) as writer:
        # Лист 1: Обработанные данные
        print(f"    Записываю лист 'Обработанные данные' ({len(df_out):,} строк)...", flush=True)
        df_out.to_excel(writer, sheet_name='Обработанные данные', index=False)
        print(f"    ✓ Лист 'Обработанные данные' ({len(df_out):,} строк)")

        # Лист 2: Свод по незавершённым кругам
        print(
            f"    Записываю лист 'Свод по незавершенным кругам' "
            f"({len(incomplete_circle_schema_df):,} строк)...", flush=True
        )
        incomplete_circle_schema_df.to_excel(
            writer, sheet_name='Свод по незавершенным кругам', index=False
        )
        print(
            f"    ✓ Лист 'Свод по незавершенным кругам' "
            f"({len(incomplete_circle_schema_df):,} строк)"
        )

        # Лист 3: Свод по завершённым кругам
        if not circle_event_schema_df.empty:
            print(f"    Записываю лист 'Свод по кругам' ({len(circle_event_schema_df):,} строк)...", flush=True)
            circle_event_schema_df.to_excel(writer, sheet_name='Свод по кругам', index=False)
            print(f"    ✓ Лист 'Свод по кругам' ({len(circle_event_schema_df):,} строк)")
        else:
            # Пустой лист всё равно создаём, чтобы структура книги была постоянной.
            pd.DataFrame(columns=[
                'Номер КТК', 'Номер круга', 'Основной маршрут',
                'Первая точка круга', 'Последняя точка круга', 'Время круга',
                'Схема участка', '№ отрезка', 'Отрезок', 'Тип движения',
                'дата начала операции', 'дата окончания операции', 'Длительность операции',
                'Схема движения по отрезкам', 'Продукт 2',
            ]).to_excel(writer, sheet_name='Свод по кругам', index=False)
            print("    ! 'Свод по кругам' пуст: завершённых кругов не найдено")

    print(f"    ✓ Файл обработанных данных: {os.path.basename(processed_file)} ({len(df_out):,} строк)")
    return processed_file
