"""Логика определения, нумерации и проверки завершённости кругов."""
import pandas as pd

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



def process_circles_and_enrich(df, original_columns, продукты):
    """Найти круги, установить признаки и подготовить финальную таблицу событий."""
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

    return df, circles_df
