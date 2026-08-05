import pandas as pd
import numpy as np
from datetime import datetime
import os
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)

MONTH_NAMES = {
    1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
    5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
    9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
}


def load_excel_safe(file_path, sheet_name=0):
    try:
        if os.path.exists(file_path):
            print(f"  Загрузка: {os.path.basename(file_path)}")
            return pd.read_excel(file_path, sheet_name=sheet_name)
        else:
            print(f"  Файл не найден: {file_path}")
            return None
    except Exception as e:
        print(f"  Ошибка при загрузке {file_path}: {e}")
        return None


def clean_string_columns(df, col_names):
    for col in col_names:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace('nan', None)
    return df


def find_all_circles_with_flags(group):
    group = group.sort_values('Дата события').reset_index(drop=True)

    if 'Состояние КТК' in group.columns:
        group['Состояние КТК'] = group['Состояние КТК'].astype(str).str.strip().replace('nan', None)

    first_point_types = {
        'Прибытие на terminal',
        'Прибытие на терминал',
        'Движение на судне (фактическая дата начала) для порта отправления',
        'Выбытие с терминала'
    }
    second_point_type = 'Прибытие на терминал'
    second_allowed_states = {'Груженый', 'груженый', 'Порожний', 'порожний', None, ''}

    group['Flag_point'] = None
    group['Номер круга'] = None
    group['Отрезок_между_флагами'] = 'Нет'
    group['Вид расчета'] = None
    group['Основной маршрут'] = None

    china_events = group[group['Локация'] == 'Китай'].copy()
    china_events['idx_in_group'] = china_events.index

    china_records = china_events.sort_values('Дата события').to_dict('records')

    if len(china_records) < 2:
        return group, []

    def is_first_point(row):
        return row['Тип движения'] in first_point_types

    def is_second_point(row):
        if row['Тип движения'] != second_point_type:
            return False
        state = row.get('Состояние КТК', '')
        return state in second_allowed_states or (pd.isna(state) and None in second_allowed_states)

    circles = []
    circle_num = 1
    i = 0
    total_china = len(china_records)

    while i < total_china:
        if not is_first_point(china_records[i]):
            i += 1
            continue

        current_date = china_records[i]['Дата события']
        j = i + 1
        while j < total_china and is_first_point(china_records[j]):
            next_date = china_records[j]['Дата события']
            if (next_date - current_date).days < 10:
                current_date = next_date
                j += 1
            else:
                break

        best_candidate = china_records[j - 1]
        first_idx = best_candidate['idx_in_group']
        first_date = best_candidate['Дата события']
        first_type = best_candidate['Тип движения']

        second_idx = None
        second_row = None
        for k in range(j, total_china):
            candidate = china_records[k]
            if not is_second_point(candidate):
                continue
            cand_idx = candidate['idx_in_group']
            if cand_idx <= first_idx:
                continue
            duration = (candidate['Дата события'] - first_date).days
            if duration < 10:
                continue

            between_locs = group.loc[first_idx + 1: cand_idx - 1, 'Локация'].dropna().tolist()
            unique_between = [loc for idx, loc in enumerate(between_locs) if idx == 0 or loc != between_locs[idx - 1]]

            if not unique_between or all(l == 'Китай' for l in unique_between):
                continue

            second_idx = cand_idx
            second_row = candidate
            break

        if second_idx is not None:
            second_date = second_row['Дата события']
            second_type = second_row['Тип движения']
            duration = (second_date - first_date).days

            route_parts = group.loc[first_idx: second_idx, 'Локация'].dropna().tolist()
            unique_route_parts = [loc for idx, loc in enumerate(route_parts) if idx == 0 or loc != route_parts[idx - 1]]

            circle_route = ' → '.join(route_parts)
            unique_route = ' → '.join(unique_route_parts)

            group.loc[first_idx:second_idx, 'Номер круга'] = circle_num
            group.loc[first_idx:second_idx, 'Отрезок_между_флагами'] = 'Да'
            group.loc[first_idx:second_idx, 'Основной маршрут'] = unique_route
            group.loc[first_idx:second_idx, 'Вид расчета'] = f"{first_type} - {second_type}"

            group.at[first_idx, 'Flag_point'] = 1
            group.at[second_idx, 'Flag_point'] = 2

            circles.append({
                'Номер круга': circle_num,
                'Начало': first_date,
                'Окончание': second_date,
                'Тип начала': first_type,
                'Тип окончания': second_type,
                'Вид расчета': f"{first_type} - {second_type}",
                'Продолжительность (дней)': duration,
                'Маршрут круга': circle_route,
                'Основной маршрут': unique_route,
                'Является кругом': True
            })

            circle_num += 1
            i = next(idx for idx, rec in enumerate(china_records) if rec['idx_in_group'] == second_idx)
        else:
            i = j

    return group, circles


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


def build_segments(df):
    """
    Строит таблицу отрезков маршрута по локациям для каждого КТК в рамках круга.

    Колонки в результате:
    - Номер КТК
    - Номер круга
    - Продукт
    - Основной маршрут
    - Тип движения (из последнего события в локации ОТКУДА)
    - Место события (из последнего события в локации ОТКУДА)
    - Локация откуда
    - Локация куда
    - Отрезок
    - Дата прибытия (первый день в локации ОТКУДА)
    - Дата выбытия (последний день в локации ОТКУДА)
    - Дней в локации = (дата выбытия - дата прибытия), если даты разные, иначе 0
    - Дней в пути = время между локациями (для информации, если нужно)
    """
    if 'Номер КТК' not in df.columns or 'Локация' not in df.columns:
        print("    ! build_segments: нет нужных колонок")
        return pd.DataFrame(), {}

    # Работаем только по строкам внутри кругов
    df_circles = df[df['Номер круга'].notna()].copy()
    print(f"    Строк внутри кругов: {len(df_circles):,}")

    if df_circles.empty:
        print("    ! build_segments: нет строк с кругами")
        return pd.DataFrame(), {}

    # Приводим Номер круга к int
    df_circles['Номер круга'] = df_circles['Номер круга'].astype(int)

    rows = []
    row_to_segment = {}  # исходный индекс строки (из df) -> текст "Отрезок" для листа "Обработанные данные"

    for (ktk, circle_num), grp in df_circles.groupby(['Номер КТК', 'Номер круга']):
        grp = grp.sort_values('Дата события')  # индекс НЕ сбрасываем - нужен для сопоставления с исходной таблицей

        # Получаем продукт и маршрут круга
        product = None
        if 'Продукт' in grp.columns:
            product_vals = grp['Продукт'].dropna()
            if not product_vals.empty:
                product = product_vals.iloc[0]

        main_route = None
        if 'Основной маршрут' in grp.columns:
            route_vals = grp['Основной маршрут'].dropna()
            if not route_vals.empty:
                main_route = route_vals.iloc[0]

        # Разбиваем на визиты с сохранением последнего события
        visits = []
        current_visit = None
        current_visit_row_indices = []  # исходные индексы строк, входящих в текущий визит
        visits_row_indices = []  # список списков индексов - по одному на каждый визит

        for idx, row in grp.iterrows():
            loc = row['Локация']
            if pd.isna(loc):
                continue

            date = row['Дата события']
            move_type = row.get('Тип движения', None)
            place = row.get('Место события', None)

            if current_visit is None:
                current_visit = {
                    'локация': loc,
                    'дата_прибытия': date,
                    'дата_выбытия': date,
                    'тип_движения': move_type,
                    'место_события': place
                }
                current_visit_row_indices = [idx]
            elif current_visit['локация'] == loc:
                # Обновляем последнюю дату, тип движения и место события
                current_visit['дата_выбытия'] = date
                current_visit['тип_движения'] = move_type
                current_visit['место_события'] = place
                current_visit_row_indices.append(idx)
            else:
                visits.append(current_visit)
                visits_row_indices.append(current_visit_row_indices)
                current_visit = {
                    'локация': loc,
                    'дата_прибытия': date,
                    'дата_выбытия': date,
                    'тип_движения': move_type,
                    'место_события': place
                }
                current_visit_row_indices = [idx]

        if current_visit is not None:
            visits.append(current_visit)
            visits_row_indices.append(current_visit_row_indices)

        # Строим отрезки
        for i in range(len(visits) - 1):
            visit_from = visits[i]
            visit_to = visits[i + 1]

            loc_from = visit_from['локация']
            loc_to = visit_to['локация']

            # Данные из локации FROM (последнее событие)
            arrival_date = visit_from['дата_прибытия']
            departure_date = visit_from['дата_выбытия']
            move_type = visit_from['тип_движения']
            place = visit_from['место_события']

            # Рассчитываем Дней в локации
            # Если дата прибытия и дата выбытия одинаковые -> 0 дней
            if arrival_date and departure_date:
                if arrival_date == departure_date:
                    days_in_location = 0
                else:
                    days_in_location = (departure_date - arrival_date).days
            else:
                days_in_location = None

            segment_label = f"{loc_from} → {loc_to}"

            rows.append({
                'Номер КТК': ktk,
                'Номер круга': circle_num,
                'Продукт': product,
                'Основной маршрут': main_route,
                'Тип движения': move_type,
                'Место события': place,
                'Локация откуда': loc_from,
                'Локация куда': loc_to,
                'Отрезок': segment_label,
                'Дата прибытия': arrival_date.strftime('%d.%m.%Y') if pd.notna(arrival_date) else None,
                'Дата выбытия': departure_date.strftime('%d.%m.%Y') if pd.notna(departure_date) else None,
                'Дней в локации': days_in_location,
                # временные "сырые" даты для последующего расчёта "Дней между локациями" по всей таблице
                '_Дата прибытия (raw)': arrival_date,
                '_Дата выбытия (raw)': departure_date,
            })

            # Все исходные строки визита "ОТКУДА" относятся к этому отрезку
            for row_idx in visits_row_indices[i]:
                row_to_segment[row_idx] = segment_label

            # Последний визит в круге ("КУДА" последнего отрезка) сам никогда не бывает "ОТКУДА",
            # поэтому его строки относим к последнему отрезку (иначе они останутся без значения)
            if i == len(visits) - 2:
                for row_idx in visits_row_indices[i + 1]:
                    row_to_segment[row_idx] = segment_label

    print(f"    Отрезков построено: {len(rows):,}")

    segments_df = pd.DataFrame(rows)
    if segments_df.empty:
        return segments_df, row_to_segment

    # Дней между локациями: дата прибытия СЛЕДУЮЩЕЙ строки (следующего отрезка)
    # минус дата выбытия ТЕКУЩЕЙ строки, для одного и того же КТК (сквозняком, без привязки к кругу —
    # т.к. переход из последнего отрезка одного круга в первый отрезок следующего круга тоже занимает время)
    segments_df['_next_arrival_raw'] = segments_df.groupby('Номер КТК')['_Дата прибытия (raw)'].shift(-1)
    mask = segments_df['_next_arrival_raw'].notna() & segments_df['_Дата выбытия (raw)'].notna()
    segments_df['Дней между локациями'] = None
    # Считаем только по календарным датам, без времени (иначе часы/минуты в "Дата события"
    # могут "съедать" целый день при округлении вниз)
    next_arrival_date_only = segments_df.loc[mask, '_next_arrival_raw'].dt.normalize()
    departure_date_only = segments_df.loc[mask, '_Дата выбытия (raw)'].dt.normalize()
    segments_df.loc[mask, 'Дней между локациями'] = (next_arrival_date_only - departure_date_only).dt.days

    segments_df = segments_df.drop(columns=['_Дата прибытия (raw)', '_Дата выбытия (raw)', '_next_arrival_raw'])
    return segments_df, row_to_segment



def save_processed_data(df, output_folder):
    """
    Файл 2: Обработанные данные
    Лист 1: Обработанные данные
    Лист 2: Отрезки маршрута (дни между локациями по кругам)
    """
    os.makedirs(output_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    processed_file = os.path.join(output_folder, f"обработанные_данные_{timestamp}.xlsx")

    print(f"\n  Сохранение обработанных данных...")
    print(f"    Колонки df: {df.columns.tolist()[:6]}...")
    print(f"    'Номер КТК' в df: {'Номер КТК' in df.columns}")

    # Строим отрезки маршрута один раз - используем и для листа "Отрезки маршрута",
    # и для колонки "Отрезок" на листе "Обработанные данные"
    print(f"    Строю отрезки маршрута...")
    print(f"    Уникальных кругов в df: {df['Номер круга'].notna().sum():,} строк с кругом")
    segments_df, row_to_segment = build_segments(df)

    # Добавляем колонку "Отрезок" в df по исходному индексу строки
    # (для строк вне кругов останется пусто - у них нет отрезка маршрута)
    df = df.copy()
    df['Отрезок'] = df.index.map(row_to_segment)

    # Порядок колонок как в образце
    desired_order = [
        'Номер КТК', 'Собственник', 'Типоразмер', 'Дата события',
        'Место события', 'Место назначения', 'Тип движения', 'Состояние КТК',
        'Грузоотправитель', 'Грузополучатель', 'Вид транспорта', 'Источник',
        'Кластер события', 'Кластер назначения', 'Собственность КТК',
        'Учетное направление', 'Перегруз',
        '№ события', 'Дней между событиями', 'Маршрут следования', 'Локация',
        'Flag_point', 'Номер круга', 'Отрезок_между_флагами', 'Вид расчета',
        'Основной маршрут', 'Отрезок', 'Первая точка', 'Последняя точка', 'Продукт',
        'Проверка круга', 'Основной маршрут (общий)', 'Количество кругов'
    ]
    final_cols = [c for c in desired_order if c in df.columns]
    extra = [c for c in df.columns if c not in final_cols]
    final_cols = final_cols + extra

    df_out = df[final_cols].copy()

    with pd.ExcelWriter(processed_file, engine='openpyxl') as writer:
        # Лист 1: Обработанные данные
        df_out.to_excel(writer, sheet_name='Обработанные данные', index=False)
        print(f"    ✓ Лист 'Обработанные данные' ({len(df_out):,} строк)")

        # Лист 2: Отрезки маршрута
        if not segments_df.empty:
            segments_df.to_excel(writer, sheet_name='Отрезки маршрута', index=False)
            print(f"    ✓ Лист 'Отрезки маршрута' ({len(segments_df):,} отрезков)")
        else:
            print(f"    ! Лист 'Отрезки маршрута' — нет данных")

    print(f"    ✓ Файл обработанных данных: {os.path.basename(processed_file)} ({len(df_out):,} строк)")
    return processed_file


def process_ktk_data():
    current_dir = os.path.dirname(os.path.abspath(__file__))

    source_path = os.path.join(current_dir, "Источник", "выгрузка Истории КТК.xlsx")
    справочник_место_path = os.path.join(current_dir, "Справочники", "Справочник_место.xlsx")
    продукты_path = os.path.join(current_dir, "Справочники", "Продукты.xlsx")

    print("=" * 60)
    print("НАЧАЛО ОБРАБОТКИ ДАННЫХ КТК")
    print(f"Рабочая директория: {current_dir}")
    print("=" * 60)

    print("\n1. Загрузка справочников...")
    справочник_место = load_excel_safe(справочник_место_path)
    продукты = load_excel_safe(продукты_path)
    if справочник_место is None or продукты is None:
        return None, None

    print(f"    Справочник мест: {len(справочник_место)} записей")
    print(f"    Справочник продуктов: {len(продукты)} записей")

    справочник_место = clean_string_columns(справочник_место, ['Место события'])
    справочник_место = справочник_место.drop_duplicates(subset=['Место события'], keep='first')

    print("\n2. Загрузка исходных данных...")
    df = load_excel_safe(source_path)
    if df is None:
        return None, None
    print(f"    Загружено {len(df):,} строк")

    df.columns = df.columns.str.strip()
    original_columns = df.columns.tolist()
    print(f"    Колонки исходника: {original_columns[:5]}...")
    if 'Номер КТК' not in original_columns:
        print(f"    ВНИМАНИЕ: 'Номер КТК' не найден! Первая колонка: {repr(original_columns[0])}")

    print("\n3. Преобразование типов данных...")
    if 'Дата события' in df.columns:
        df['Дата события'] = pd.to_datetime(df['Дата события'], errors='coerce')
    df = clean_string_columns(df, ['Номер КТК', 'Место события', 'Тип движения', 'Состояние КТК'])

    print("\n4. Фильтрация данных...")
    if 'Номер КТК' in df.columns:
        df = df[df['Номер КТК'] != 'Номер КТК']

    print("\n5. Объединение со справочником мест...")
    if 'Место события' in справочник_место.columns and 'Кластер события' in справочник_место.columns:
        dict_places = справочник_место[['Место события', 'Кластер события']].copy()
        dict_places = dict_places.rename(columns={'Кластер события': 'Локация'})
        if 'Локация' in df.columns:
            df = df.drop(columns=['Локация'])
        df = df.merge(dict_places, on='Место события', how='left')
    else:
        print("  ВНИМАНИЕ: Не найдены нужные колонки в справочнике мест!")
        df['Локация'] = 'Неизвестно'

    print("\n6. Нумерация событий и расчёт дней...")
    df = df.sort_values(['Номер КТК', 'Дата события']).reset_index(drop=True)
    df['№ события'] = df.groupby('Номер КТК').cumcount() + 1
    # Считаем только по календарным датам, без времени внутри суток
    # (иначе, например, 07.09 21:41 -> 09.09 01:50 даёт "1 день 4 часа", и .dt.days
    # округляет вниз до 1, хотя по календарным датам это 2 дня)
    df['Дней между событиями'] = df.groupby('Номер КТК')['Дата события'].transform(
        lambda s: s.dt.normalize().diff().dt.days
    )
    df['Дней между событиями'] = df['Дней между событиями'].fillna(0)

    print("\n7. Расчёт полного маршрута...")

    def calculate_route(locations):
        locs = [loc for loc in locations if pd.notna(loc)]
        unique_locs = [loc for idx, loc in enumerate(locs) if idx == 0 or loc != locs[idx - 1]]
        return ' → '.join(unique_locs)

    route_df = df.groupby('Номер КТК')['Локация'].apply(calculate_route).reset_index(name='Маршрут следования')
    df = df.merge(route_df, on='Номер КТК', how='left')

    print("\n8. Поиск кругов и простановка флагов...")
    all_circles_list = []
    df_list = []
    for ktk, grp in df.groupby('Номер КТК'):
        processed_group, circles = find_all_circles_with_flags(grp)
        df_list.append(processed_group)
        for circle in circles:
            circle.update({
                'Номер КТК': ktk,
                'Проверка круга': 'Круг' if circle['Является кругом'] else 'Не круг',
                'Тип отправления': circle.pop('Тип начала'),
                'Тип прибытия': circle.pop('Тип окончания')
            })
            if isinstance(circle['Начало'], pd.Timestamp):
                circle['Начало'] = circle['Начало'].strftime('%d.%m.%Y')
            if isinstance(circle['Окончание'], pd.Timestamp):
                circle['Окончание'] = circle['Окончание'].strftime('%d.%m.%Y')
            all_circles_list.append(circle)

    df = pd.concat(df_list, ignore_index=True)

    df['Первая точка'] = None
    df['Последняя точка'] = None

    circle_mask = df['Номер круга'].notna()
    if circle_mask.any():
        circle_dates = df[circle_mask].groupby(['Номер КТК', 'Номер круга'])['Дата события'].agg(
            ['min', 'max']).reset_index()
        df = df.merge(circle_dates, on=['Номер КТК', 'Номер круга'], how='left')
        df['Первая точка'] = np.where(df['Дата события'] == df['min'], df['min'].dt.strftime('%d.%m.%Y'), None)
        df['Последняя точка'] = np.where(df['Дата события'] == df['max'], df['max'].dt.strftime('%d.%m.%Y'), None)
        df = df.drop(columns=['min', 'max'])

    circles_df = pd.DataFrame(all_circles_list)

    print("\n9. Присоединение продуктов из справочника...")
    if 'Основной маршрут' in продукты.columns and 'Продукт' in продукты.columns:
        product_dict = dict(zip(продукты['Основной маршрут'].astype(str).str.strip(), продукты['Продукт']))
        if 'Основной маршрут' in df.columns:
            df['Продукт'] = df['Основной маршрут'].map(product_dict)
        if not circles_df.empty and 'Основной маршрут' in circles_df.columns:
            circles_df['Продукт'] = circles_df['Основной маршрут'].map(product_dict)

    print("\n10. Добавление общей проверки круга...")

    def check_complete_circle(grp):
        china_events = grp[grp['Локация'] == 'Китай']['Дата события']
        grp['Проверка круга'] = 'Не круг'
        if len(china_events) >= 2 and china_events.min() != china_events.max():
            if ((grp['Дата события'] > china_events.min()) & (grp['Дата события'] < china_events.max()) & (
                    grp['Локация'] != 'Китай')).any():
                grp['Проверка круга'] = 'Круг'
        return grp

    # pandas новых версий убирает ключ groupby из колонок — обходим через список
    df_list_10 = []
    for ktk_val, grp in df.groupby('Номер КТК'):
        result = check_complete_circle(grp)
        if 'Номер КТК' not in result.columns:
            result.insert(0, 'Номер КТК', ktk_val)
        df_list_10.append(result)
    df = pd.concat(df_list_10, ignore_index=True)

    print("\n11-12. Расчёт маршрутов и количества кругов...")

    def extract_main_route_and_count(route):
        if pd.isna(route) or not route:
            return "не полный круг", 0
        parts = route.split(' → ')
        china_indices = [i for i, part in enumerate(parts) if part == 'Китай']
        if len(china_indices) < 2:
            return "не полный круг", 0
        main_route = ' → '.join(parts[china_indices[0]:china_indices[-1] + 1])
        circles_count = sum(1 for i in range(len(china_indices) - 1) if china_indices[i + 1] - china_indices[i] > 1)
        return main_route, circles_count

    df[['Основной маршрут (общий)', 'Количество кругов']] = df['Маршрут следования'].apply(
        lambda x: pd.Series(extract_main_route_and_count(x))
    )

    print("\n13. Формирование итоговой таблицы...")
    new_columns = [
        '№ события', 'Дней между событиями', 'Маршрут следования', 'Локация',
        'Flag_point', 'Номер круга', 'Отрезок_между_флагами', 'Вид расчета',
        'Основной маршрут', 'Первая точка', 'Последняя точка',
        'Продукт', 'Проверка круга', 'Основной маршрут (общий)', 'Количество кругов'
    ]
    # Гарантируем что Номер КТК всегда первая
    all_cols = ['Номер КТК'] + [c for c in original_columns if c != 'Номер КТК'] + new_columns
    final_cols = [c for c in dict.fromkeys(all_cols) if c in df.columns]
    df = df[final_cols]

    sort_keys = [k for k in ['Номер КТК', 'Дата события'] if k in df.columns]
    if sort_keys:
        df = df.sort_values(sort_keys).reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)

    print("\n" + "=" * 60)
    print("ОБРАБОТКА ЗАВЕРШЕНА")
    print("=" * 60)
    print(f"Обработано контейнеров: {df['Номер КТК'].nunique() if 'Номер КТК' in df.columns else 'Успешно'}")
    print(f"Найдено кругов всего: {len(circles_df):,}")

    return df, circles_df


if __name__ == "__main__":
    try:
        output_folder = os.path.dirname(os.path.abspath(__file__))

        result_df, circles_df = process_ktk_data()
        if result_df is not None and len(result_df) > 0:
            # Файл 1: Отчёт (группировка + СВОД + детализация)
            save_report(result_df, circles_df, output_folder)

            # Файл 2: Обработанные данные (один лист со всеми колонками)
            save_processed_data(result_df, output_folder)

            print("\n✅ Оба файла успешно сформированы!")
        else:
            print("\n❌ Ошибка: Данные пусты или не были загружены.")
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА ВЫПОЛНЕНИЯ: {e}")
        import traceback
        traceback.print_exc()

    input("\nНажмите Enter для выхода из программы...")