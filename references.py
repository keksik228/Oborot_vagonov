"""Загрузка, очистка и сопоставление исходных данных со справочниками."""
import os
import unicodedata
import pandas as pd

from config import BASE_DIR, SOURCE_FILE, PLACES_FILE, PRODUCTS_FILE, SEGMENTS_FILE, SCHEMES_FILE

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

def _normalize_place_key(value):
    """Нормализует место для сопоставления: регистр, NBSP и повторяющиеся пробелы."""
    if pd.isna(value):
        return None
    value = unicodedata.normalize('NFKC', str(value)).replace('\u00a0', ' ')
    value = ' '.join(value.split()).casefold()
    return value or None

PLACE_CLUSTER_ALIASES = {
    'yujie shaanxi': 'Китай',
}

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

def load_and_prepare_input():
    """Загрузить входные данные, очистить события и сопоставить локации."""
    current_dir = str(BASE_DIR)

    source_path = str(SOURCE_FILE)
    справочник_место_path = str(PLACES_FILE)
    продукты_path = str(PRODUCTS_FILE)
    отрезки_path = str(SEGMENTS_FILE)
    схемы_path = str(SCHEMES_FILE)

    print("\n1. Загрузка справочников...")
    справочник_место = load_excel_safe(справочник_место_path)
    продукты = load_excel_safe(продукты_path)
    отрезки = load_excel_safe(отрезки_path)
    if (справочник_место is None or
        продукты is None or
        отрезки is None):
        return None, None, None, {}

    missing_segment_cols = _missing_segment_reference_columns(отрезки)
    if missing_segment_cols:
        print("    КРИТИЧЕСКАЯ ОШИБКА: в Отрезки.xlsx отсутствуют колонки: "
              + ', '.join(missing_segment_cols))
        return None, None, None, {}

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
        return None, None, None, {}
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

    return df, original_columns, продукты, product2_dict
