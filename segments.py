"""Построение отрезков маршрута и свода операций по кругам."""
import pandas as pd
import numpy as np

from circles import (
    _append_synthetic_rows,
    _find_missing_circle_endpoint_rows,
    _normalize_circle_key,
    _circle_boundary_lookup,
)
from references import (
    _normalize_schema_key,
    map_segment_schemes,
)

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

    # Принудительно приводим сырые даты к datetime64: после concat/shift pandas
    # иногда оставляет object, из-за чего обращение к .dt вызывает AttributeError.
    segments_df['_Дата прибытия (raw)'] = pd.to_datetime(
        segments_df['_Дата прибытия (raw)'], errors='coerce'
    )
    segments_df['_Дата выбытия (raw)'] = pd.to_datetime(
        segments_df['_Дата выбытия (raw)'], errors='coerce'
    )

    # Дней между локациями: дата прибытия СЛЕДУЮЩЕЙ строки (следующего отрезка)
    # минус дата выбытия ТЕКУЩЕЙ строки, для одного и того же КТК (сквозняком, без привязки к кругу —
    # т.к. переход из последнего отрезка одного круга в первый отрезок следующего круга тоже занимает время)
    segments_df['_next_arrival_raw'] = pd.to_datetime(
        segments_df.groupby('Номер КТК', sort=False)['_Дата прибытия (raw)'].shift(-1),
        errors='coerce'
    )
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

def build_circle_event_schema(
    df, segment_dict, circle_date_range=None, product2_dict=None, circles_df=None,
    include_incomplete=False,
):
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

    # Ключ завершённого круга присутствует в circles_df с определёнными началом и концом.
    # По умолчанию строим «Свод по кругам» только для завершённых кругов.
    # При include_incomplete=True строим отдельный свод только для незавершённых.
    # Незавершённые не получают синтетического возвращения в Китай.
    all_circle_df = df[df['Номер круга'].notna()].copy()
    if all_circle_df.empty:
        return pd.DataFrame()

    boundary_lookup = _circle_boundary_lookup(circles_df)
    completed_circle_keys = {
        key for key, (start_date, end_date) in boundary_lookup.items()
        if pd.notna(start_date) and pd.notna(end_date)
    }

    row_circle_keys = [
        _normalize_circle_key(ktk, circle_num)
        for ktk, circle_num in zip(all_circle_df['Номер КТК'], all_circle_df['Номер круга'])
    ]
    all_circle_keys = {key for key in row_circle_keys if key is not None}
    incomplete_circle_keys = all_circle_keys - completed_circle_keys

    if include_incomplete:
        selected_keys = incomplete_circle_keys
        if not selected_keys:
            print("    Для 'Свод по незавершённым кругам' нет незавершённых кругов.", flush=True)
            return pd.DataFrame()
        print(
            f"    Для 'Свод по незавершённым кругам': кругов {len(selected_keys):,}.",
            flush=True,
        )
    else:
        selected_keys = completed_circle_keys
        if not selected_keys:
            print("    Для 'Свод по кругам' нет завершённых кругов; незавершённые исключены.", flush=True)
            return pd.DataFrame()
        print(
            f"    Для 'Свод по кругам': завершённых кругов {len(selected_keys):,}; "
            f"незавершённых исключено {len(incomplete_circle_keys):,}.",
            flush=True,
        )

    selected_mask = [key in selected_keys for key in row_circle_keys]
    circle_df = all_circle_df.loc[selected_mask].copy()
    if circle_df.empty:
        return pd.DataFrame()

    # Восстанавливаем финальное прибытие в Китай только для завершённых кругов.
    if not include_incomplete:
        synthetic_endpoints = _find_missing_circle_endpoint_rows(df, circle_df, circles_df)
        if not synthetic_endpoints.empty:
            circle_df = _append_synthetic_rows(circle_df, synthetic_endpoints, df)
            print(f"    Для 'Свод по кругам' добавлено конечных точек в Китай: {len(synthetic_endpoints):,}")

    # Восстанавливаем датетайм-тип после concat с синтетическими строками.
    circle_df['Дата события'] = pd.to_datetime(circle_df['Дата события'], errors='coerce')
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

    # Границы круга: для завершённых используем точные даты из circles_df.
    # Для незавершённых первая точка = первое событие круга, последняя точка =
    # последнее известное событие в выгрузке. Дополнительно считаем «Время в движении»
    # как количество календарных дней от первой до последней наблюдаемой точки.
    circle_df['Первая точка круга'] = None
    circle_df['Последняя точка круга'] = None
    if include_incomplete:
        circle_df['Последняя локация'] = None
        circle_df['Время в движении'] = None
    else:
        circle_df['Время круга'] = None

    for (ktk, circle_num), grp_idx in circle_df.groupby(['Номер КТК', 'Номер круга'], sort=False).groups.items():
        date_key = _normalize_circle_key(ktk, circle_num)
        if not include_incomplete and circle_date_range is not None and date_key in circle_date_range.index:
            start_date = circle_date_range.loc[date_key, 'min']
            end_date = circle_date_range.loc[date_key, 'max']
            if pd.notna(start_date):
                circle_df.loc[grp_idx, 'Первая точка круга'] = start_date.strftime('%d.%m.%Y')
            if pd.notna(end_date):
                circle_df.loc[grp_idx, 'Последняя точка круга'] = end_date.strftime('%d.%m.%Y')
            if pd.notna(start_date) and pd.notna(end_date):
                circle_df.loc[grp_idx, 'Время круга'] = (end_date.normalize() - start_date.normalize()).days
        else:
            group_rows = circle_df.loc[grp_idx].sort_values('Дата события', kind='mergesort')
            group_dates = pd.to_datetime(group_rows['Дата события'], errors='coerce').dropna()
            if not group_dates.empty:
                start_date = group_dates.min()
                end_date = group_dates.max()
                circle_df.loc[grp_idx, 'Первая точка круга'] = start_date.strftime('%d.%m.%Y')
                circle_df.loc[grp_idx, 'Последняя точка круга'] = end_date.strftime('%d.%m.%Y')
                if include_incomplete:
                    circle_df.loc[grp_idx, 'Время в движении'] = (
                        end_date.normalize() - start_date.normalize()
                    ).days

            if include_incomplete and 'Локация' in group_rows.columns:
                observed_locations = group_rows['Локация'].dropna()
                if not observed_locations.empty:
                    # Последняя локация определяется фактически последней точкой,
                    # а не разбором текстовой строки «Основной маршрут».
                    circle_df.loc[grp_idx, 'Последняя локация'] = str(
                        observed_locations.iloc[-1]
                    ).strip()

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
    ]
    if include_incomplete:
        result_cols.extend([
            'Последняя локация', 'Первая точка круга',
            'Последняя точка круга', 'Время в движении',
        ])
    else:
        result_cols.extend([
            'Первая точка круга', 'Последняя точка круга', 'Время круга',
        ])
    result_cols.extend([
        'Схема участка', '№ отрезка', 'Отрезок', 'Тип движения',
        'дата начала операции', 'дата окончания операции', 'Длительность операции',
        'Схема движения по отрезкам', 'Продукт 2'
    ])
    return circle_df[result_cols].copy()

