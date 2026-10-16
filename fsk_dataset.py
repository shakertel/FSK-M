ALLOWED_READ_TABLES = {'movies','movie_concepts','movie_special_phrases'}

def load_all(table, columns):
    if table not in ALLOWED_READ_TABLES:
        raise RuntimeError('Table outside movie-only read routes')
    rows = []; offset = 0
    # Total count prevents silent early completion if a project caps pages below 1000.
    expected = sb.table(table).select('id', count='exact', head=True).execute().count
    if expected is None:
        raise RuntimeError('Exact database count unavailable')
    while len(rows) < expected:
        page = (sb.table(table).select(columns).order('id')
                .range(offset, offset + 999).execute().data or [])
        if not page:
            raise RuntimeError(f'Incomplete read: {table}: {len(rows)}/{expected}')
        rows.extend(page); offset += len(page)
        if len(rows) % 5000 == 0 or len(rows) >= expected:
            print(f'📥 {table}: {len(rows):,}/{expected:,}', flush=True)
    if len(rows) != expected or len({str(x['id']) for x in rows}) != expected:
        raise RuntimeError(f'Dataset changed during load; rerun this cell: {table}')
    return rows

movies = load_all('movies', 'id,title,description,recommendation_tags,imdb_rating,year,countries,genres,poster_url,actors,directors_creators,runtime_minutes,digimoviez_url,imdb_url,analysis_status,comments_analyzed')
concepts = load_all('movie_concepts', 'id,series_id,concept')
phrases = load_all('movie_special_phrases', 'id,series_id,phrase')
pmap = defaultdict(list)
for p in phrases:
    if p.get('phrase'): pmap[str(p['series_id'])].append(p['phrase'])
cmap = defaultdict(list)
for c in concepts:
    if c.get('concept'): cmap[str(c['series_id'])].append(c['concept'])
for movie in movies:
    movie['concepts'] = cmap.get(str(movie['id']), [])
    movie['special_phrases'] = pmap.get(str(movie['id']), [])
assert movies, 'جدول فیلم خالی است.'
movie_lookup = {str(x['id']): x for x in movies}
title_counts = Counter(x['title'] for x in movies)
# Compatibility aliases only: every row below is ONE real movie, never a clone.
simulated = []
for movie in movies:
    item = dict(movie)
    item.update(sid=str(movie['id']), source_id=str(movie['id']), source_title=movie['title'])
    if title_counts[movie['title']] > 1:
        item['title'] = movie['title'] + ' [SIM-REAL-' + str(movie['id']) + ']'
    simulated.append(item)
by_sim_title = {x['title']: x for x in simulated}
assert len(by_sim_title) == len(movies), 'Ambiguous title identifier'
DATASET_SHA = hashlib.sha256(json.dumps(simulated, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
