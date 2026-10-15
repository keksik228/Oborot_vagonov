import pandas as pd
import numpy as np
from datetime import datetime
import os
import warnings
import unicodedata

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


def _reference_file_path(current_dir, filename):
    """Находит справочник в папке «Справочник» или «Справочники»."""
    for folder_name in ("Справочник", "Справочники"):
        candidate = os.path.join(current_dir, folder_name, filename)
        if os.path.exists(candidate):
            return candidate
    # Возвращаем ожидаемый путь, чтобы сообщение об ошибке было понятным.
    return os.path.join(current_dir, "Справочник", filename)


def clean_string_columns(df, col_names):
    for col in col_names:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().replace('nan', None)
    return df


def _normalize_place_key(value):
    """Нормализует место для сопоставления: регистр, NBSP и повторяющиеся пробелы."""
    if pd.isna(value):
        return None
    value = unicodedata.normalize('NFKC', str(value)).replace('\u00a0', ' ')
    value = ' '.join(value.split()).casefold()
    return value or None


# Известные сокращённые варианты названий мест, которые встречались в выгрузках.
# Точное совпадение со справочником всегда имеет приоритет над этим списком.
PLACE_CLUSTER_ALIASES = {
    'yujie shaanxi': 'Китай',
}


def _parse_circle_date(value):
    """Разбирает дату границы круга, которая может быть строкой или Timestamp."""
    if pd.isna(value):
        return pd.NaT
    return pd.to_datetime(value, dayfirst=True, errors='coerce')


def _normalize_circle_key(ktk, circle_num):
    """Единый ключ круга независимо от int/float в столбце номера круга."""
    if pd.isna(ktk) or pd.isna(circle_num):
        return None
    try:
        circle_num = int(float(circle_num))
    except (TypeError, ValueError):
        return None
    return str(ktk).strip(), circle_num


def _circle_boundary_lookup(circles_df):
    """Возвращает точные начальную и конечную даты из списка найденных кругов."""
    lookup = {}
    if circles_df is None or circles_df.empty:
        return lookup
    required = {'Номер КТК', 'Номер круга', 'Начало', 'Окончание'}
    if not required.issubset(circles_df.columns):
        return lookup

    for _, row in circles_df.iterrows():
        key = _normalize_circle_key(row['Номер КТК'], row['Номер круга'])
        if key is None:
            continue
        lookup[key] = (
            _parse_circle_date(row['Начало']),
            _parse_circle_date(row['Окончание'])
        )
    return lookup


def _build_circle_date_range(circles_df):
    """Строит диапазоны дат из результатов поиска кругов, а не из уже размеченных строк."""
    lookup = _circle_boundary_lookup(circles_df)
    if not lookup:
        return None
    records = [
        {'Номер КТК': key[0], 'Номер круга': key[1], 'min': dates[0], 'max': dates[1]}
        for key, dates in lookup.items()
    ]
    boundary_df = pd.DataFrame(records)
    return boundary_df.set_index(['Номер КТК', 'Номер круга'])[['min', 'max']]


def _find_missing_circle_endpoint_rows(all_events, circle_events, circles_df):
    """Находит конечное прибытие в Китай, если оно стало началом следующего круга.

    Важно для скорости: раньше для каждого круга заново фильтровалась вся
    таблица событий. При десятках тысяч кругов это приводило к сотням миллионов
    и миллиардам проверок. Теперь события в Китае индексируются один раз по
    ключу (Номер КТК, календарная дата), после чего кандидат выбирается напрямую.
    """
    empty = pd.DataFrame(columns=all_events.columns)
    if (circles_df is None or circles_df.empty or all_events.empty or circle_events.empty
            or 'Дата события' not in all_events.columns or 'Локация' not in all_events.columns
            or 'Номер КТК' not in all_events.columns):
        return empty

    boundary_lookup = _circle_boundary_lookup(circles_df)
    if not boundary_lookup:
        return empty

    metadata_lookup = {}
    for _, meta in circles_df.iterrows():
        key = _normalize_circle_key(meta.get('Номер КТК'), meta.get('Номер круга'))
        if key is not None:
            metadata_lookup[key] = meta

    all_dates = pd.to_datetime(all_events['Дата события'], errors='coerce')
    all_ktk = all_events['Номер КТК'].astype(str).str.strip()
    all_locations = all_events['Локация'].astype(str).str.strip()

    # Однократно строим индекс всех событий прибытия/нахождения в Китае.
    china_mask = all_locations.eq('Китай') & all_dates.notna()
    china_events = all_events.loc[china_mask].copy()
    if china_events.empty:
        return empty
    china_events['_endpoint_ktk_key'] = all_ktk.loc[china_mask].to_numpy()
    china_events['_endpoint_date_key'] = all_dates.loc[china_mask].dt.normalize().to_numpy()
    endpoint_lookup = {}
    for (ktk_key, date_key), candidates in china_events.groupby(
        ['_endpoint_ktk_key', '_endpoint_date_key'], sort=False
    ):
        endpoint_lookup[(str(ktk_key).strip(), pd.Timestamp(date_key))] = candidates.drop(
            columns=['_endpoint_ktk_key', '_endpoint_date_key']
        )

    synthetic_rows = []
    for (ktk, circle_num), grp in circle_events.groupby(['Номер КТК', 'Номер круга'], sort=False):
        key = _normalize_circle_key(ktk, circle_num)
        if key is None or key not in boundary_lookup:
            continue

        start_date, end_date = boundary_lookup[key]
        if pd.isna(end_date):
            continue
        end_date_key = pd.Timestamp(end_date).normalize()

        grp_dates = pd.to_datetime(grp['Дата события'], errors='coerce')
        grp_sorted = grp.assign(_circle_date_tmp=grp_dates).sort_values(
            '_circle_date_tmp', kind='mergesort'
        )
        if not grp_sorted.empty:
            last_row = grp_sorted.iloc[-1]
            last_date = pd.to_datetime(last_row['Дата события'], errors='coerce')
            last_loc = str(last_row.get('Локация', '')).strip()
            # Если последняя строка круга уже является прибытием в Китай на конечную дату,
            # копия не нужна.
            if (pd.notna(last_date) and last_date.normalize() == end_date_key
                    and last_loc == 'Китай'):
                continue
            # Не добавляем искусственную границу раньше событий, уже попавших в этот круг.
            valid_group_dates = grp_dates.dropna()
            if not valid_group_dates.empty and valid_group_dates.max().normalize() > end_date_key:
                continue

        candidates = endpoint_lookup.get((key[0], end_date_key))
        if candidates is None or candidates.empty:
            # Поиск круга всегда заканчивается на китайском событии. Если его нельзя
            # найти по данным, не выдумываем событие и дату.
            continue
        candidates = candidates.copy()

        meta = metadata_lookup.get(key)
        target_move = meta.get('Тип прибытия') if meta is not None else None
        if pd.notna(target_move) and 'Тип движения' in candidates.columns:
            typed = candidates[candidates['Тип движения'].astype(str).str.strip().eq(str(target_move).strip())]
            if not typed.empty:
                candidates = typed

        endpoint = candidates.iloc[-1].copy()
        endpoint['Номер круга'] = key[1]
        if 'Основной маршрут' in grp.columns:
            route_values = grp['Основной маршрут'].dropna()
            if not route_values.empty:
                endpoint['Основной маршрут'] = route_values.iloc[0]
        if 'Flag_point' in endpoint.index:
            endpoint['Flag_point'] = 2
        if 'Отрезок_между_флагами' in endpoint.index:
            endpoint['Отрезок_между_флагами'] = 'Да'
        if meta is not None and 'Вид расчета' in endpoint.index and pd.notna(meta.get('Вид расчета')):
            endpoint['Вид расчета'] = meta.get('Вид расчета')
        if 'Первая точка' in endpoint.index and pd.notna(start_date):
            endpoint['Первая точка'] = start_date.strftime('%d.%m.%Y')
        if 'Последняя точка' in endpoint.index:
            endpoint['Последняя точка'] = end_date.strftime('%d.%m.%Y')
        synthetic_rows.append(endpoint)

    if not synthetic_rows:
        return empty
    return pd.DataFrame(synthetic_rows, columns=all_events.columns)

def _append_synthetic_rows(base_df, synthetic_df, index_source_df=None):
    """Добавляет синтетические граничные строки с новыми индексами."""
    if synthetic_df is None or synthetic_df.empty:
        return base_df
    result = base_df.copy()
    synthetic_df = synthetic_df.copy()
    source = index_source_df if index_source_df is not None else base_df
    try:
        next_idx = int(max(source.index)) + 1 if len(source.index) else 0
    except (TypeError, ValueError):
        next_idx = len(result)
    synthetic_df.index = range(next_idx, next_idx + len(synthetic_df))
    return pd.concat([result, synthetic_df], axis=0, sort=False)


def find_all_circles_with_flags(group):
    group = group.sort_values('Дата события', kind='mergesort').reset_index(drop=True)

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

    china_records = china_events.sort_values('Дата события', kind='mergesort').to_dict('records')

    if not china_records:
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
    last_completed_end_idx = -1

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

            last_completed_end_idx = second_idx
            circle_num += 1
            i = next(idx for idx, rec in enumerate(china_records) if rec['idx_in_group'] == second_idx)
        else:
            i = j

    # Нумеруем также текущий незавершённый круг. Полный круг требует возвращения
    # в Китай, но после отправления из Китая события должны уже получать следующий
    # номер, даже если на дату выгрузки возврата ещё не было.
    open_start = None
    for rec in china_records:
        rec_idx = rec['idx_in_group']
        # Конечное прибытие в Китай может одновременно быть стартовой границей
        # следующего круга (как 15.08.2025 для круга 2). Поэтому последнюю точку
        # завершённого круга тоже рассматриваем как возможное начало текущего круга.
        if rec_idx < last_completed_end_idx or not is_first_point(rec):
            continue

        # Считаем круг начавшимся, только если после точки старта есть событие
        # за пределами Китая. Это не позволяет превратить одиночную запись в Китае
        # в новый круг без фактического продолжения маршрута.
        later_locations = group.loc[rec_idx + 1:, 'Локация']
        has_departed_china = later_locations.notna().any() and later_locations.astype(str).str.strip().ne('Китай').any()
        if has_departed_china:
            open_start = rec
            break

    if open_start is not None:
        first_idx = open_start['idx_in_group']
        first_date = open_start['Дата события']
        first_type = open_start['Тип движения']
        ongoing_rows = group.index >= first_idx
        route_parts = group.loc[ongoing_rows, 'Локация'].dropna().tolist()
        unique_route_parts = [loc for idx, loc in enumerate(route_parts) if idx == 0 or loc != route_parts[idx - 1]]
        ongoing_route = ' → '.join(unique_route_parts)

        group.loc[ongoing_rows, 'Номер круга'] = circle_num
        group.loc[ongoing_rows, 'Отрезок_между_флагами'] = 'Да'
        group.loc[ongoing_rows, 'Основной маршрут'] = ongoing_route
        group.loc[ongoing_rows, 'Вид расчета'] = f"{first_type} - незавершённый круг"
        group.at[first_idx, 'Flag_point'] = 1

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


def build_segments(df, circles_df=None):
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
    synthetic_endpoints = _find_missing_circle_endpoint_rows(df, df_circles, circles_df)
    if not synthetic_endpoints.empty:
        df_circles = _append_synthetic_rows(df_circles, synthetic_endpoints, df)
        print(f"    Добавлено конечных точек кругов в Китай: {len(synthetic_endpoints):,}")
    print(f"    Строк внутри кругов: {len(df_circles):,}")

    if df_circles.empty:
        print("    ! build_segments: нет строк с кругами")
        return pd.DataFrame(), {}

    # Приводим Номер круга к int
    df_circles['Номер круга'] = df_circles['Номер круга'].astype(int)

    rows = []
    row_to_segment = {}  # исходный индекс строки (из df) -> текст "Отрезок" для листа "Обработанные данные"

    for (ktk, circle_num), grp in df_circles.groupby(['Номер КТК', 'Номер круга']):
        grp = grp.sort_values('Дата события', kind='mergesort')  # индекс НЕ сбрасываем - нужен для сопоставления с исходной таблицей

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
                    'тип_движения_прибытия': move_type,  # тип ПЕРВОГО события визита (не перезаписывается)
                    'тип_движения': move_type,  # тип ПОСЛЕДНЕГО события визита (выбытие)
                    'место_события': place
                }
                current_visit_row_indices = [idx]
            elif current_visit['локация'] == loc:
                # Обновляем последнюю дату, тип движения (выбытие) и место события.
                # 'тип_движения_прибытия' НЕ трогаем - там должен остаться тип первого события визита
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
                    'тип_движения_прибытия': move_type,
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
            move_type = visit_from['тип_движения']  # тип выбытия (последнее событие визита)
            move_type_arrival = visit_from['тип_движения_прибытия']  # тип прибытия (первое событие визита)
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
                'Тип движения прибытия': move_type_arrival,
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


        # Добавляем отдельную конечную строку круга: "Китай" с одинаковыми
        # датами прибытия/выбытия (для последней точки следующего круга не берём).
        if visits:
            final_visit = visits[-1]
            final_loc = final_visit['локация']
            final_date = final_visit['дата_выбытия']
            final_label = str(final_loc).strip()
            rows.append({
                'Номер КТК': ktk,
                'Номер круга': circle_num,
                'Продукт': product,
                'Основной маршрут': main_route,
                'Тип движения прибытия': final_visit['тип_движения_прибытия'],
                'Тип движения': final_visit['тип_движения'],
                'Место события': final_visit['место_события'],
                'Локация откуда': final_loc,
                'Локация куда': None,
                'Отрезок': final_label,
                'Дата прибытия': final_date.strftime('%d.%m.%Y') if pd.notna(final_date) else None,
                'Дата выбытия': final_date.strftime('%d.%m.%Y') if pd.notna(final_date) else None,
                'Дней в локации': 0,
                '_Дата прибытия (raw)': final_date,
                '_Дата выбытия (raw)': final_date,
            })
            # Строки последнего события относятся к конечной локации ("Китай"),
            # а не к предыдущему переходу ("ДВ → Китай").
            for row_idx in visits_row_indices[-1]:
                row_to_segment[row_idx] = final_label

    print(f"    Отрезков построено: {len(rows):,}")

    segments_df = pd.DataFrame(rows)
    if segments_df.empty:
        return segments_df, row_to_segment

    # Дней между локациями: дата прибытия СЛЕДУЮЩЕЙ строки (следующего отрезка)
    # минус дата выбытия ТЕКУЩЕЙ строки, для одного и того же КТК (сквозняком, без привязки к кругу —
    # т.к. переход из последнего отрезка одного круга в первый отрезок следующего круга тоже занимает время)
    segments_df['_next_arrival_raw'] = segments_df.groupby('Номер КТК')['_Дата прибытия (raw)'].shift(-1)
    # Интервал считаем только для строки реального перехода. Конечная точка
    # круга (например, "Китай") не должна забирать на себя время до следующего круга.
    has_destination = segments_df['Локация куда'].notna()
    mask = (
        segments_df['_next_arrival_raw'].notna()
        & segments_df['_Дата выбытия (raw)'].notna()
        & has_destination
    )
    segments_df['Дней между локациями'] = None
    segments_df.loc[~has_destination, 'Дней между локациями'] = 0
    # Считаем только по календарным датам, без времени.
    next_arrival_date_only = segments_df.loc[mask, '_next_arrival_raw'].dt.normalize()
    departure_date_only = segments_df.loc[mask, '_Дата выбытия (raw)'].dt.normalize()
    segments_df.loc[mask, 'Дней между локациями'] = (next_arrival_date_only - departure_date_only).dt.days

    segments_df = segments_df.drop(columns=['_Дата прибытия (raw)', '_Дата выбытия (raw)', '_next_arrival_raw'])
    return segments_df, row_to_segment



def _classify_arrival_departure(text):
    """Приводит текст 'Тип движения' к одной из двух категорий: 'Прибытие' или 'Выбытие'."""
    if pd.isna(text):
        return 'Выбытие'
    text_low = str(text).strip().lower()
    if 'прибыт' in text_low:
        return 'Прибытие'
    return 'Выбытие'


def build_circle_operations_summary(segments_df, segment_dict, circle_date_range=None):
    """
    Строит свод по кругам: для каждого круга (Номер КТК + Номер круга) -
    последовательность операций (Отрезок схемы) в хронологическом порядке.

    circle_date_range: DataFrame с индексом (Номер КТК, Номер круга) и колонками
    'min'/'max' - минимальная и максимальная "Дата события" ИЗ ИСХОДНЫХ данных
    (не из сегментов!) для этого круга. Нужен, потому что последняя точка круга
    (например, финальное прибытие в Китай) никогда не становится отдельным
    отрезком в segments_df - её дату можно получить только из исходных событий.

    Номер (№) присваивается по порядку ПЕРВОГО появления каждой уникальной
    "Отрезок схемы" внутри круга. На каждую "Отрезок схемы" - ВСЕГДА ровно
    2 строки: "Прибытие" и "Выбытие" (даже если по факту в круге встретился
    только один из этих типов - вторая строка идёт с нулём). Три метрики времени:
      - "Время круга" - сколько дней КТК шёл по кругу целиком (от первого
        события до последнего события в круге), одно значение на весь круг;
      - "Время в отрезке" - время в пути между локациями (было "Дней между
        локациями"), сумма по каждой уникальной "Отрезок схемы";
      - "Время на операции" - время нахождения в локации, между прибытием
        и выбытием (было "Дней в локации"), сумма по каждой уникальной
        "Отрезок схемы" отдельно для "Прибытие" и для "Выбытие".
    """
    if segments_df.empty:
        return pd.DataFrame()

    df = segments_df.copy()
    print("    Сопоставляю 'Отрезок схемы' по справочнику...", flush=True)
    overload_series = df['Перегруз'] if 'Перегруз' in df.columns else pd.Series(None, index=df.index)
    direction_series = df['Учетное направление'] if 'Учетное направление' in df.columns else pd.Series(None, index=df.index)
    df['Отрезок схемы'] = map_segment_schemes(
        df['Отрезок'], overload_series, direction_series, segment_dict
    )
    print("    Сопоставление отрезков завершено.", flush=True)
    df['Дней между локациями'] = pd.to_numeric(df['Дней между локациями'], errors='coerce').fillna(0)
    df['Дней в локации'] = pd.to_numeric(df['Дней в локации'], errors='coerce').fillna(0)

    rows = []
    for (ktk, circle_num), grp in df.groupby(['Номер КТК', 'Номер круга'], sort=False):
        main_route = grp['Основной маршрут'].iloc[0] if 'Основной маршрут' in grp.columns else None

        # Время круга: берём точный диапазон по исходным событиям, если он передан
        circle_time = None
        if circle_date_range is not None and (ktk, circle_num) in circle_date_range.index:
            start_date = circle_date_range.loc[(ktk, circle_num), 'min']
            end_date = circle_date_range.loc[(ktk, circle_num), 'max']
            if pd.notna(start_date) and pd.notna(end_date):
                circle_time = (end_date.normalize() - start_date.normalize()).days

        # № = порядок первого появления каждой уникальной "Отрезок схемы" в этом круге
        order_map = {}
        next_num = 1
        for scheme in grp['Отрезок схемы']:
            if pd.isna(scheme):
                continue
            if scheme not in order_map:
                order_map[scheme] = next_num
                next_num += 1

        # Сумма "Дней между локациями" (время в отрезке) по каждой уникальной "Отрезок схемы" в круге
        agg_segment = grp.groupby('Отрезок схемы', sort=False, dropna=True)['Дней между локациями'].sum().reset_index()

        # "Время на операции" разбиваем по РЕАЛЬНЫМ (литеральным) значениям "Тип движения" -
        # только те статусы, что фактически встретились в круге для этой "Отрезок схемы", без пустых строк
        agg_op = grp.groupby(
            ['Отрезок схемы', 'Тип движения'],
            sort=False, dropna=False
        )['Дней в локации'].sum().reset_index()
        agg_op['№'] = agg_op['Отрезок схемы'].map(order_map)

        agg = agg_op.merge(
            agg_segment[['Отрезок схемы', 'Дней между локациями']],
            on='Отрезок схемы', how='left'
        )
        agg = agg.sort_values(['№', 'Тип движения'])
        seen_schemes = set()
        segment_time_col = []
        for scheme, val in zip(agg['Отрезок схемы'], agg['Дней между локациями']):
            if scheme not in seen_schemes:
                segment_time_col.append(val)
                seen_schemes.add(scheme)
            else:
                segment_time_col.append(None)
        agg['Время в отрезке'] = segment_time_col

        for _, r in agg.iterrows():
            rows.append({
                'Номер КТК': ktk,
                'Номер круга': circle_num,
                'Основной маршрут': main_route,
                'Время круга': circle_time,
                '№': r['№'],
                'Отрезок схемы': r['Отрезок схемы'],
                'Время в отрезке': r['Время в отрезке'],
                'Тип движения': r['Тип движения'],
                'Время на операции': r['Дней в локации'],
            })

        # Итоговая строка по кругу
        rows.append({
            'Номер КТК': ktk,
            'Номер круга': circle_num,
            'Основной маршрут': f"{main_route} Итог" if main_route else "Итог",
            'Время круга': circle_time,
            '№': None,
            'Отрезок схемы': None,
            'Время в отрезке': agg_segment['Дней между локациями'].sum(),
            'Тип движения': None,
            'Время на операции': agg_op['Дней в локации'].sum(),
        })

    return pd.DataFrame(rows)



def _normalize_schema_key(value):
    """Нормализует строку схемы для сопоставления со справочником Схемы.xlsx."""
    if pd.isna(value):
        return None

    value = str(value).strip()
    # Убираем лишние пробелы вокруг стрелок и внутри текста.
    parts = [part.strip() for part in value.split('→')]
    parts = [' '.join(part.split()) for part in parts]
    return ' → '.join(parts)


def _normalize_overload(value):
    """Нормализует значение признака перегруза Да/Нет."""
    if pd.isna(value):
        return None

    value = unicodedata.normalize('NFKC', str(value)).replace('\u00a0', ' ')
    value = ' '.join(value.split()).lower()
    if value in {'', 'nan', 'none', '(пусто)', 'пусто'}:
        return None
    if value in {'да', 'yes', 'true', '1'}:
        return 'да'
    if value in {'нет', 'no', 'false', '0'}:
        return 'нет'
    return value


def _normalize_segment_key(value):
    """Нормализует значение перехода, не меняя смысл самого отрезка."""
    if pd.isna(value):
        return None
    value = unicodedata.normalize('NFKC', str(value)).replace('\u00a0', ' ')
    value = ' '.join(value.split())
    if not value or value.casefold() in {'nan', 'none'}:
        return None
    if '→' in value:
        value = ' → '.join(' '.join(part.split()) for part in value.split('→'))
    return value.casefold()


def _normalize_account_direction(value):
    """Нормализует учетное направление. «(пусто)» означает пустое поле,
    а не универсальное правило: оно совпадает только с пустым направлением в данных.
    """
    if pd.isna(value):
        return None
    value = unicodedata.normalize('NFKC', str(value)).replace('\u00a0', ' ')
    value = ' '.join(value.split())
    if not value or value.casefold() in {'nan', 'none', '(пусто)', 'пусто'}:
        return None
    return value.casefold()


def _segment_reference_columns(segment_df):
    """Возвращает реальные названия столбцов справочника (поддерживает старые заголовки)."""
    if segment_df is None:
        return None, None, None, None
    segment_df.columns = segment_df.columns.astype(str).str.strip()

    def pick(candidates):
        return next((name for name in candidates if name in segment_df.columns), None)

    segment_col = pick(('Следующий отрезок', 'Отрезок (сл. назначение)', 'Отрезок'))
    overload_col = pick(('Перегруз',))
    direction_col = pick(('Учетное направление', 'Учётное направление'))
    scheme_col = pick(('Отрезок схемы', 'отрезок схемы', 'Схема участка'))
    return segment_col, overload_col, direction_col, scheme_col


def _missing_segment_reference_columns(segment_df):
    segment_col, overload_col, direction_col, scheme_col = _segment_reference_columns(segment_df)
    missing = []
    if segment_col is None:
        missing.append('Следующий отрезок')
    if overload_col is None:
        missing.append('Перегруз')
    if direction_col is None:
        missing.append('Учетное направление')
    if scheme_col is None:
        missing.append('Отрезок схемы')
    return missing


def build_segment_dict(segment_df):
    """Создает точный справочник соответствий по трем параметрам:
    (Следующий отрезок, Перегруз, Учетное направление) -> Отрезок схемы.

    Универсального fallback нет: пока справочник заполняется, незаполненное
    или отсутствующее правило оставляет «Отрезок схемы» пустым.
    """
    segment_col, overload_col, direction_col, scheme_col = _segment_reference_columns(segment_df)
    required = (segment_col, overload_col, direction_col, scheme_col)
    if any(col is None for col in required):
        missing = _missing_segment_reference_columns(segment_df)
        print('    КРИТИЧЕСКАЯ ОШИБКА: в справочнике отрезков отсутствуют колонки: '
              + ', '.join(missing))
        return {'exact': {}}

    exact = {}
    conflicting_keys = 0
    for _, row in segment_df.iterrows():
        segment_key = _normalize_segment_key(row.get(segment_col))
        scheme_value = row.get(scheme_col)
        if segment_key is None or pd.isna(scheme_value):
            continue
        scheme_value = ' '.join(str(scheme_value).strip().split())
        if not scheme_value or scheme_value.casefold() in {'nan', 'none'}:
            continue

        overload_key = _normalize_overload(row.get(overload_col))
        direction_key = _normalize_account_direction(row.get(direction_col))
        key = (segment_key, overload_key, direction_key)

        if key in exact and exact[key] != scheme_value:
            conflicting_keys += 1
            continue
        exact[key] = scheme_value

    print(f"    Правил отрезков загружено: {len(exact):,}")
    if conflicting_keys:
        print(f"    ВНИМАНИЕ: конфликтующих повторных правил пропущено: {conflicting_keys:,}")
    print("    Сопоставление использует: Следующий отрезок + Перегруз + Учетное направление.")
    return {'exact': exact}


def get_segment_scheme(segment, overload, direction, segment_dict):
    """Возвращает схему только при точном совпадении всех трех параметров."""
    segment_key = _normalize_segment_key(segment)
    if segment_key is None:
        return None
    key = (
        segment_key,
        _normalize_overload(overload),
        _normalize_account_direction(direction),
    )
    return segment_dict.get('exact', {}).get(key)


def map_segment_schemes(segment_series, overload_series, direction_series, segment_dict):
    """Векторизованное сопоставление схем по трем параметрам; без подстановок по догадке."""
    if overload_series is None:
        overload_series = pd.Series(None, index=segment_series.index)
    if direction_series is None:
        direction_series = pd.Series(None, index=segment_series.index)

    segment_keys = segment_series.map(_normalize_segment_key).tolist()
    overload_keys = overload_series.map(_normalize_overload).tolist()
    direction_keys = direction_series.map(_normalize_account_direction).tolist()
    exact = segment_dict.get('exact', {})

    mapped_values = (
        exact.get((segment_key, overload_key, direction_key)) if segment_key is not None else None
        for segment_key, overload_key, direction_key
        in zip(segment_keys, overload_keys, direction_keys)
    )
    return pd.Series(mapped_values, index=segment_series.index, dtype='object')


def build_circle_event_schema(df, segment_dict, circle_date_range=None, product2_dict=None, circles_df=None):
    """
    Построчная (по каждому СОБЫТИЮ, а не по визиту/отрезку) раскладка внутри круга:
      - "Отрезок" = Локация текущей строки -> Локация следующей строки (та же логика,
        что и "Следующий отрезок" на листе "Обработанные данные"), включая самопереходы
        (X -> X) внутри одной локации;
      - "Схема участка" = "отрезок схемы" из справочника с учетом "Перегруз";
      - "№ отрезка" растёт на 1 при каждой смене пары (Схема участка, Отрезок)
        относительно предыдущей строки круга - даже если такая же пара уже была раньше;
      - "дата начала/окончания операции" и "Длительность операции" - дата этой строки
        и дата следующей строки (в пределах того же КТК), разница в календарных днях;
      - "Схема движения по отрезкам" - склеенная через " → " последовательность
        уникальных подряд идущих "Схема участка" по всему кругу (одна и та же строка
        на каждую строку этого круга).
    """
    if df.empty or 'Номер круга' not in df.columns:
        return pd.DataFrame()

    # На лист «Свод по кругам» выводим только завершённые круги.
    # Незавершённые круги остаются в «Обработанных данных», но не попадают в свод.
    # Список завершённых границ берётся из circles_df, где записаны найденные
    # пары «старт в Китае — возвращение в Китай». При этом исходный df не фильтруем:
    # он нужен ниже для восстановления конечной точки в Китае, которая иногда
    # одновременно становится началом следующего (незавершённого) круга.
    all_circle_df = df[df['Номер круга'].notna()].copy()
    if all_circle_df.empty:
        return pd.DataFrame()

    boundary_lookup = _circle_boundary_lookup(circles_df)
    completed_circle_keys = {
        key for key, (start_date, end_date) in boundary_lookup.items()
        if pd.notna(start_date) and pd.notna(end_date)
    }
    if not completed_circle_keys:
        print("    Для 'Свод по кругам' нет завершённых кругов; незавершённые исключены.", flush=True)
        return pd.DataFrame()

    row_circle_keys = [
        _normalize_circle_key(ktk, circle_num)
        for ktk, circle_num in zip(all_circle_df['Номер КТК'], all_circle_df['Номер круга'])
    ]
    completed_mask = [key in completed_circle_keys for key in row_circle_keys]
    circle_df = all_circle_df.loc[completed_mask].copy()

    all_circle_keys = {key for key in row_circle_keys if key is not None}
    excluded_count = len(all_circle_keys - completed_circle_keys)
    print(
        f"    Для 'Свод по кругам': завершённых кругов {len(completed_circle_keys):,}; "
        f"незавершённых исключено {excluded_count:,}.",
        flush=True,
    )
    if circle_df.empty:
        return pd.DataFrame()

    synthetic_endpoints = _find_missing_circle_endpoint_rows(df, circle_df, circles_df)
    if not synthetic_endpoints.empty:
        circle_df = _append_synthetic_rows(circle_df, synthetic_endpoints, df)
        print(f"    Для 'Свод по кругам' добавлено конечных точек в Китай: {len(synthetic_endpoints):,}")

    circle_df = circle_df.sort_values(['Номер КТК', 'Дата события'], kind='mergesort').reset_index(drop=True)

    # Следующую локацию и дату ищем только ВНУТРИ того же круга.
    # Это важно: последняя строка круга не должна получать переход
    # к первой строке следующего круга или оставаться без завершающего отрезка.
    group_cols = ['Номер КТК', 'Номер круга']
    next_location = circle_df.groupby(group_cols)['Локация'].shift(-1)
    next_date = circle_df.groupby(group_cols)['Дата события'].shift(-1)

    has_next_loc = next_location.notna()
    has_cur_loc = circle_df['Локация'].notna()

    # Последняя строка каждого круга определяется именно по границе группы,
    # а не по отсутствию следующей строки во всём КТК.
    is_last_in_circle = ~circle_df.duplicated(subset=group_cols, keep='last')

    circle_df['Отрезок'] = None
    normal_mask = (~is_last_in_circle) & has_next_loc & has_cur_loc
    circle_df.loc[normal_mask, 'Отрезок'] = (
        circle_df.loc[normal_mask, 'Локация'].astype(str)
        + ' → '
        + next_location.loc[normal_mask].astype(str)
    )

    # Для последней строки круга отрезок равен конечной локации.
    # Например: ... → Китай, а последняя строка получает просто "Китай".
    last_mask = is_last_in_circle & has_cur_loc
    circle_df.loc[last_mask, 'Отрезок'] = circle_df.loc[last_mask, 'Локация'].astype(str)

    circle_df['дата начала операции'] = circle_df['Дата события']
    circle_df['дата окончания операции'] = next_date

    # Для последней строки круга дата окончания равна дате начала этой же строки.
    circle_df.loc[last_mask, 'дата окончания операции'] = circle_df.loc[last_mask, 'Дата события']
    mask_dates = circle_df['дата начала операции'].notna() & circle_df['дата окончания операции'].notna()
    circle_df['Длительность операции'] = None
    circle_df.loc[mask_dates, 'Длительность операции'] = (
        circle_df.loc[mask_dates, 'дата окончания операции'].dt.normalize()
        - circle_df.loc[mask_dates, 'дата начала операции'].dt.normalize()
    ).dt.days

    # Схема участка определяется только по справочнику Отрезки.xlsx с учетом Перегруз.
    # Автоматическое формирование названий "Межсервис <Локация>" отключено.
    print("    Сопоставляю 'Схема участка' для строк кругов...", flush=True)
    overload_series = circle_df['Перегруз'] if 'Перегруз' in circle_df.columns else pd.Series(None, index=circle_df.index)
    direction_series = circle_df['Учетное направление'] if 'Учетное направление' in circle_df.columns else pd.Series(None, index=circle_df.index)
    circle_df['Схема участка'] = map_segment_schemes(
        circle_df['Отрезок'], overload_series, direction_series, segment_dict
    )
    print("    'Схема участка' рассчитана.", flush=True)

    # № отрезка: растёт при смене пары (Схема участка, Отрезок) относительно предыдущей строки круга
    nums = pd.Series(index=circle_df.index, dtype='float64')
    for (ktk, circle_num), grp in circle_df.groupby(['Номер КТК', 'Номер круга'], sort=False):
        prev_pair = None
        n = 0
        for idx, row in grp.iterrows():
            pair = (row['Схема участка'], row['Отрезок'])
            if pair != prev_pair:
                n += 1
                prev_pair = pair
            nums.loc[idx] = n
    circle_df['№ отрезка'] = nums

    # Основной маршрут (берём как есть из исходных данных, если колонка есть)
    if 'Основной маршрут' in circle_df.columns:
        pass  # уже есть в circle_df как колонка исходных данных
    else:
        circle_df['Основной маршрут'] = None

    # Время круга, Первая точка круга, Последняя точка круга - из точного диапазона дат
    # по исходным событиям (circle_date_range), если он передан
    circle_df['Первая точка круга'] = None
    circle_df['Последняя точка круга'] = None
    circle_df['Время круга'] = None
    for (ktk, circle_num), grp_idx in circle_df.groupby(['Номер КТК', 'Номер круга'], sort=False).groups.items():
        date_key = _normalize_circle_key(ktk, circle_num)
        if circle_date_range is not None and date_key in circle_date_range.index:
            start_date = circle_date_range.loc[date_key, 'min']
            end_date = circle_date_range.loc[date_key, 'max']
            if pd.notna(start_date):
                circle_df.loc[grp_idx, 'Первая точка круга'] = start_date.strftime('%d.%m.%Y')
            if pd.notna(end_date):
                circle_df.loc[grp_idx, 'Последняя точка круга'] = end_date.strftime('%d.%m.%Y')
            if pd.notna(start_date) and pd.notna(end_date):
                circle_df.loc[grp_idx, 'Время круга'] = (end_date.normalize() - start_date.normalize()).days
        else:
            # Для незавершённого круга указываем старт, но не придумываем
            # конечную дату и длительность по дате последнего наблюдаемого события.
            group_dates = pd.to_datetime(circle_df.loc[grp_idx, 'Дата события'], errors='coerce').dropna()
            if not group_dates.empty:
                circle_df.loc[grp_idx, 'Первая точка круга'] = group_dates.min().strftime('%d.%m.%Y')

    # Схема движения по отрезкам: последовательность "Схема участка" по кругу.
    # Подряд идущие одинаковые операции схлопываются в одну,
    # причем сравнение выполняется по нормализованному названию (без влияния
    # лишних пробелов в начале/конце и вокруг стрелок).
    scheme_seq_map = {}
    for (ktk, circle_num), grp in circle_df.groupby(['Номер КТК', 'Номер круга'], sort=False):
        seq = []
        prev_val = None
        for val in grp['Схема участка']:
            if pd.isna(val):
                continue
            normalized_val = _normalize_schema_key(val)
            if normalized_val is None:
                continue
            if normalized_val != prev_val:
                seq.append(normalized_val)
                prev_val = normalized_val
        scheme_seq_map[(ktk, circle_num)] = ' → '.join(seq)
    circle_df['Схема движения по отрезкам'] = circle_df.apply(
        lambda r: scheme_seq_map.get((r['Номер КТК'], r['Номер круга'])), axis=1
    )

    # Продукт 2 определяется по полной схеме движения по отрезкам.
    # Сопоставление делаем по нормализованному тексту, чтобы не зависеть
    # от лишних пробелов в начале/конце строки и вокруг стрелок.
    if product2_dict is not None:
        circle_df['Продукт 2'] = circle_df['Схема движения по отрезкам'].map(
            lambda x: product2_dict.get(_normalize_schema_key(x)) if pd.notna(x) else None
        )
    else:
        circle_df['Продукт 2'] = None

    result_cols = [
        'Номер КТК', 'Номер круга', 'Основной маршрут',
        'Первая точка круга', 'Последняя точка круга', 'Время круга',
        'Схема участка', '№ отрезка', 'Отрезок', 'Тип движения',
        'дата начала операции', 'дата окончания операции', 'Длительность операции',
        'Схема движения по отрезкам', 'Продукт 2'
    ]
    return circle_df[result_cols].copy()


def save_processed_data(df, output_folder, product2_dict=None, circles_df=None):
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
    segments_df, row_to_segment = build_segments(df, circles_df)

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

    current_dir = os.path.dirname(os.path.abspath(__file__))

    отрезки = load_excel_safe(_reference_file_path(current_dir, "Отрезки.xlsx"))
    if отрезки is None:
        print("    КРИТИЧЕСКАЯ ОШИБКА: не удалось загрузить справочник Отрезки.xlsx")
        return None, None, {}

    missing_segment_cols = _missing_segment_reference_columns(отрезки)
    if missing_segment_cols:
        print("    КРИТИЧЕСКАЯ ОШИБКА: в Отрезки.xlsx отсутствуют колонки: "
              + ', '.join(missing_segment_cols))
        print("    Ожидаются параметры: Следующий отрезок, Перегруз, Учетное направление, Отрезок схемы.")
        return None, None, {}

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

    # Свод по кругам: построчная раскладка с восстановленной конечной точкой в Китае.
    print("    Формирую лист 'Свод по кругам'...", flush=True)
    circle_event_schema_df = build_circle_event_schema(
        df, segment_dict, circle_date_range, product2_dict, circles_df
    )
    print(f"    'Свод по кругам' подготовлен: {len(circle_event_schema_df):,} строк.", flush=True)

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

        # Лист 2: Отрезки маршрута
        if not segments_df.empty:
            print(f"    Записываю лист 'Отрезки маршрута' ({len(segments_df):,} строк)...", flush=True)
            segments_df.to_excel(writer, sheet_name='Отрезки маршрута', index=False)
            print(f"    ✓ Лист 'Отрезки маршрута' ({len(segments_df):,} отрезков)")
        else:
            print(f"    ! Лист 'Отрезки маршрута' — нет данных")

        # Лист 3: Свод по кругам
        if not circle_event_schema_df.empty:
            print(f"    Записываю лист 'Свод по кругам' ({len(circle_event_schema_df):,} строк)...", flush=True)
            circle_event_schema_df.to_excel(writer, sheet_name='Свод по кругам', index=False)
            print(f"    ✓ Лист 'Свод по кругам' ({len(circle_event_schema_df):,} строк)")
        else:
            print(f"    ! Лист 'Свод по кругам' — нет данных")

    print(f"    ✓ Файл обработанных данных: {os.path.basename(processed_file)} ({len(df_out):,} строк)")
    return processed_file


def process_ktk_data():
    current_dir = os.path.dirname(os.path.abspath(__file__))

    source_path = os.path.join(current_dir, "Источник", "выгрузка Истории КТК.xlsx")
    справочник_место_path = _reference_file_path(current_dir, "Справочник_место.xlsx")
    продукты_path = _reference_file_path(current_dir, "Продукты.xlsx")
    отрезки_path = _reference_file_path(current_dir, "Отрезки.xlsx")
    схемы_path = _reference_file_path(current_dir, "Схемы.xlsx")

    print("=" * 60)
    print("НАЧАЛО ОБРАБОТКИ ДАННЫХ КТК")
    print(f"Рабочая директория: {current_dir}")
    print("=" * 60)

    print("\n1. Загрузка справочников...")
    справочник_место = load_excel_safe(справочник_место_path)
    продукты = load_excel_safe(продукты_path)
    отрезки = load_excel_safe(отрезки_path)
    if (справочник_место is None or
        продукты is None or
        отрезки is None):
        return None, None, {}

    missing_segment_cols = _missing_segment_reference_columns(отрезки)
    if missing_segment_cols:
        print("    КРИТИЧЕСКАЯ ОШИБКА: в Отрезки.xlsx отсутствуют колонки: "
              + ', '.join(missing_segment_cols))
        print("    Ожидаются параметры: Следующий отрезок, Перегруз, Учетное направление, Отрезок схемы.")
        return None, None, {}

    print(f"    Справочник мест: {len(справочник_место)} записей")
    print(f"    Справочник продуктов: {len(продукты)} записей")

    # Справочник схем для "Продукт 2".
    # Файл не является обязательным для основной обработки: если его нет,
    # "Продукт 2" останется пустым.
    схемы = load_excel_safe(схемы_path)
    product2_dict = {}
    if схемы is not None:
        required_schema_cols = {'Схема движения по отрезкам', 'Продукт 2'}
        if required_schema_cols.issubset(схемы.columns):
            схемы = clean_string_columns(
                схемы,
                ['Схема движения по отрезкам', 'Продукт 2']
            )
            схемы['_schema_key'] = схемы['Схема движения по отрезкам'].map(_normalize_schema_key)
            схемы = схемы[
                схемы['_schema_key'].notna() &
                схемы['Продукт 2'].notna()
            ].drop_duplicates(subset=['_schema_key'], keep='first')
            product2_dict = dict(zip(схемы['_schema_key'], схемы['Продукт 2']))
            print(f"    Справочник схем: {len(product2_dict)} записей")
        else:
            print("    ВНИМАНИЕ: В Схемы.xlsx должны быть колонки 'Схема движения по отрезкам' и 'Продукт 2'")

    справочник_место = clean_string_columns(справочник_место, ['Место события', 'Кластер события'])
    справочник_место = справочник_место.drop_duplicates(subset=['Место события'], keep='first')

    print("\n2. Загрузка исходных данных...")
    df = load_excel_safe(source_path)
    if df is None:
        return None, None, {}
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
        dict_places['_place_key'] = dict_places['Место события'].map(_normalize_place_key)
        dict_places['Кластер события'] = dict_places['Кластер события'].astype(str).str.strip()
        dict_places = dict_places[
            dict_places['_place_key'].notna()
            & dict_places['Кластер события'].notna()
            & dict_places['Кластер события'].ne('')
            & dict_places['Кластер события'].str.casefold().ne('nan')
        ].drop_duplicates(subset=['_place_key'], keep='first')
        place_lookup = dict(zip(dict_places['_place_key'], dict_places['Кластер события']))

        # Дополнительные ключи для сокращённых названий, если они являются
        # однозначным хвостом более полного названия из справочника.
        # Если разные кластеры используют один хвост, такой алиас не применяется.
        alias_clusters = {}
        for full_key, cluster in place_lookup.items():
            words = full_key.split()
            for start in range(1, max(1, len(words) - 1)):
                alias = ' '.join(words[start:])
                if len(alias) >= 8 and len(alias.split()) >= 2:
                    alias_clusters.setdefault(alias, set()).add(cluster)
        place_alias_lookup = {
            alias: next(iter(clusters))
            for alias, clusters in alias_clusters.items()
            if len(clusters) == 1
        }
        for alias, cluster in PLACE_CLUSTER_ALIASES.items():
            place_alias_lookup.setdefault(_normalize_place_key(alias), cluster)

        if 'Локация' in df.columns:
            df = df.drop(columns=['Локация'])
        place_keys = df['Место события'].map(_normalize_place_key)
        df['Локация'] = place_keys.map(place_lookup)
        need_alias = df['Локация'].isna() & place_keys.notna()
        df.loc[need_alias, 'Локация'] = place_keys.loc[need_alias].map(place_alias_lookup)

        alias_count = int((need_alias & df['Локация'].notna()).sum())
        print(f"    Совпадений со справочником мест: {(df['Локация'].notna().sum() - alias_count):,} строк")
        if alias_count:
            print(f"    Сопоставлено по однозначным вариантам названия места: {alias_count:,} строк")

        unmapped = df.loc[df['Локация'].isna() & df['Место события'].notna(), 'Место события']
        if not unmapped.empty:
            missing_places = unmapped.value_counts().head(15)
            print(f"    ВНИМАНИЕ: {unmapped.nunique():,} названий мест не сопоставлено со справочником. Примеры:")
            for place, count in missing_places.items():
                print(f"      - {place}: {count:,} строк")
    else:
        print("  ВНИМАНИЕ: Не найдены нужные колонки в справочнике мест!")
        df['Локация'] = 'Неизвестно'

    print("\n6. Нумерация событий и расчёт дней...")
    # kind='mergesort' - устойчивая сортировка: строки с одинаковой "Дата события"
    # сохраняют исходный взаимный порядок (обычный quicksort этого не гарантирует
    # и может "перемешать" такие строки между этим шагом и финальной сортировкой в конце)
    df = df.sort_values(['Номер КТК', 'Дата события'], kind='mergesort').reset_index(drop=True)
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
    circles_df = pd.DataFrame(all_circles_list)

    df['Первая точка'] = None
    df['Последняя точка'] = None

    circle_mask = df['Номер круга'].notna()
    if circle_mask.any():
        boundary_lookup = _circle_boundary_lookup(circles_df)
        # Завершённые круги используют точные границы из circles_df.
        for row_idx in df.index[circle_mask]:
            row = df.loc[row_idx]
            key = _normalize_circle_key(row['Номер КТК'], row['Номер круга'])
            if key not in boundary_lookup:
                continue
            start_date, end_date = boundary_lookup[key]
            if pd.notna(start_date):
                df.at[row_idx, 'Первая точка'] = start_date.strftime('%d.%m.%Y')
            if pd.notna(end_date):
                df.at[row_idx, 'Последняя точка'] = end_date.strftime('%d.%m.%Y')

        # У незавершённого текущего круга нет конечной точки в Китае: показываем
        # дату старта, а дату окончания оставляем пустой, не подменяя её последним событием.
        completed_keys = set(boundary_lookup)
        for (ktk, circle_num), idxs in df[circle_mask].groupby(['Номер КТК', 'Номер круга'], sort=False).groups.items():
            key = _normalize_circle_key(ktk, circle_num)
            if key in completed_keys:
                continue
            grp = df.loc[list(idxs)].sort_values('Дата события', kind='mergesort')
            dates = pd.to_datetime(grp['Дата события'], errors='coerce').dropna()
            if not dates.empty:
                start_text = dates.iloc[0].strftime('%d.%m.%Y')
                df.loc[list(idxs), 'Первая точка'] = start_text
                df.loc[list(idxs), 'Последняя точка'] = None


    print("\n9. Присоединение продуктов из справочника...")
    # Операция больше не рассчитывается через внешний справочник.
    # Оставляем колонку в выгрузке для совместимости со старыми шаблонами.
    if 'Операция' not in df.columns:
        df['Операция'] = None

    if 'Основной маршрут' in продукты.columns and 'Продукт' in продукты.columns:
        product_dict = dict(zip(продукты['Основной маршрут'].astype(str).str.strip(), продукты['Продукт']))
        if 'Основной маршрут' in df.columns:
            df['Продукт'] = df['Основной маршрут'].map(product_dict)
        if not circles_df.empty and 'Основной маршрут' in circles_df.columns:
            circles_df['Продукт'] = circles_df['Основной маршрут'].map(product_dict)

    print("\n10. Проверка только завершённых кругов...")

    # В circles_df попадают только круги, у которых найдены обе границы:
    # старт и возвращение в Китай. Номер открытого текущего круга может быть
    # присвоен строкам df для маршрутизации, но он НЕ означает, что круг завершён.
    # Раньше проверка запускалась на уровне всего контейнера: если у контейнера
    # когда-либо был хотя бы один полный круг, все строки этого контейнера,
    # включая незавершённый третий/четвёртый круг, получали признак "Круг".
    # Теперь признак ставится отдельно по паре (Номер КТК, Номер круга),
    # сопоставленной с реально найденными завершёнными кругами.
    df['Проверка круга'] = 'Не круг'
    completed_circle_keys = set()
    if circles_df is not None and not circles_df.empty:
        for _, circle_row in circles_df.iterrows():
            if ('Является кругом' in circles_df.columns
                    and not bool(circle_row.get('Является кругом', False))):
                continue
            circle_key = _normalize_circle_key(
                circle_row.get('Номер КТК'), circle_row.get('Номер круга')
            )
            if circle_key is not None:
                completed_circle_keys.add(circle_key)

    if completed_circle_keys and 'Номер круга' in df.columns and 'Номер КТК' in df.columns:
        ktk_keys = df['Номер КТК'].astype(str).str.strip()
        circle_numbers = pd.to_numeric(df['Номер круга'], errors='coerce')
        row_keys = pd.MultiIndex.from_arrays([ktk_keys, circle_numbers])
        completed_mask = row_keys.isin(completed_circle_keys)
        df.loc[completed_mask, 'Проверка круга'] = 'Круг'

    print(
        f"    Завершённых кругов для проверки: {len(completed_circle_keys):,}; "
        f"строк с признаком 'Круг': {(df['Проверка круга'] == 'Круг').sum():,}; "
        f"строк 'Не круг': {(df['Проверка круга'] == 'Не круг').sum():,}"
    )

    print("\n11-12. Расчёт маршрутов и количества кругов...")

    def extract_main_route_and_count(route):
        if pd.isna(route) or not route:
            return "не полный круг", 0

        parts = [part.strip() for part in str(route).split(' → ') if part.strip()]
        china_indices = [i for i, part in enumerate(parts) if part == 'Китай']
        if not china_indices:
            return "не полный круг", 0

        # Полные круги — маршруты между последовательными посещениями Китая,
        # в которых есть хотя бы одна промежуточная локация.
        circles_count = sum(
            1 for i in range(len(china_indices) - 1)
            if china_indices[i + 1] - china_indices[i] > 1
        )

        # Если после последнего Китая маршрут продолжается в другую локацию,
        # считаем это начавшимся, но пока незавершённым кругом.
        last_china_idx = china_indices[-1]
        has_open_tail = any(part != 'Китай' for part in parts[last_china_idx + 1:])
        if has_open_tail:
            circles_count += 1

        if len(china_indices) < 2:
            return "не полный круг", circles_count

        main_route = ' → '.join(parts[china_indices[0]:china_indices[-1] + 1])
        return main_route, circles_count

    df[['Основной маршрут (общий)', 'Количество кругов']] = df['Маршрут следования'].apply(
        lambda x: pd.Series(extract_main_route_and_count(x))
    )

    print("\n13. Формирование итоговой таблицы...")
    new_columns = [
        '№ события', 'Дней между событиями', 'Маршрут следования', 'Локация',
        'Flag_point', 'Номер круга', 'Отрезок_между_флагами', 'Вид расчета',
        'Основной маршрут', 'Первая точка','Операция', 'Последняя точка',
        'Продукт', 'Проверка круга', 'Основной маршрут (общий)', 'Количество кругов'
    ]
    # Гарантируем что Номер КТК всегда первая
    all_cols = ['Номер КТК'] + [c for c in original_columns if c != 'Номер КТК'] + new_columns
    final_cols = [c for c in dict.fromkeys(all_cols) if c in df.columns]
    df = df[final_cols]

    sort_keys = [k for k in ['Номер КТК', 'Дата события'] if k in df.columns]
    if sort_keys:
        # mergesort - та же устойчивая сортировка, что и на шаге 6, чтобы порядок строк
        # с одинаковой "Дата события" не изменился относительно уже посчитанных
        # "№ события" и "Дней между событиями"
        df = df.sort_values(sort_keys, kind='mergesort').reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)

    print("\n" + "=" * 60)
    print("ОБРАБОТКА ЗАВЕРШЕНА")
    print("=" * 60)
    print(f"Обработано контейнеров: {df['Номер КТК'].nunique() if 'Номер КТК' in df.columns else 'Успешно'}")
    print(f"Найдено завершённых кругов: {len(circles_df):,}")
    if 'Номер круга' in df.columns:
        print(f"Пронумеровано строк (включая незавершённый текущий круг): {df['Номер круга'].notna().sum():,}")

    return df, circles_df, product2_dict


if __name__ == "__main__":
    try:
        output_folder = os.path.dirname(os.path.abspath(__file__))

        result_df, circles_df, product2_dict = process_ktk_data()
        if result_df is not None and len(result_df) > 0:
            # Файл 1: Отчёт (группировка + СВОД + детализация)
            save_report(result_df, circles_df, output_folder)

            # Файл 2: Обработанные данные (один лист со всеми колонками)
            save_processed_data(result_df, output_folder, product2_dict, circles_df)

            print("\n✅ Оба файла успешно сформированы!")
        else:
            print("\n❌ Ошибка: Данные пусты или не были загружены.")
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА ВЫПОЛНЕНИЯ: {e}")
        import traceback
        traceback.print_exc()

    input("\nНажмите Enter для выхода из программы...")
