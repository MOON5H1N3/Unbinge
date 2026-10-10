def parse_release_days(raw):
    if not raw:
        return []
    try:
        return sorted({int(x) for x in str(raw).split(',') if x.strip() != ''})
    except ValueError:
        return []

WEEKDAY_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

def suggest_release_day(shows, genre_lookup, candidate_genres=None):
    day_counts = {d: 0 for d in range(7)}
    day_genres = {d: set() for d in range(7)}
    candidate_genre_set = set(candidate_genres or [])

    if candidate_genre_set:
        for s in shows:
            genres = set(genre_lookup.get(s.get('tvdb_id'), [])) if s.get('tvdb_id') else set()
            for d in parse_release_days(s['release_days']):
                day_counts[d] += 1
                day_genres[d] |= genres
    else:
        for s in shows:
            for d in parse_release_days(s['release_days']):
                day_counts[d] += 1

    lightest = min(day_counts.values())
    tied_days = [d for d in range(7) if day_counts[d] == lightest]

    if len(tied_days) == 1 or not candidate_genre_set:
        best_day = tied_days[0]
    else:
        best_day = min(tied_days, key=lambda d: (len(day_genres[d] & candidate_genre_set), d))

    if lightest == 0:
        reason = f"nothing else drips on {WEEKDAY_NAMES[best_day]} yet."
    else:
        reason = f"{WEEKDAY_NAMES[best_day]} is your lightest day ({lightest} show{'s' if lightest != 1 else ''} already there)."
        if candidate_genre_set and len(tied_days) > 1 and not (day_genres[best_day] & candidate_genre_set):
            reason += " it's also clear of shows in the same genre."

    return best_day, reason


# Test 1: no shows at all -> mon, "nothing else drips"
print(suggest_release_day([], {}))

# Test 2: thu has 2 shows, everything else empty -> any empty day (mon, tied_days[0]=0)
shows = [
    {'release_days': '3', 'tvdb_id': 1},
    {'release_days': '3', 'tvdb_id': 2},
]
print(suggest_release_day(shows, {}))

# Test 3: mon/tue/wed each have 1 show, thu/fri/sat/sun empty -> pick thu (first empty)
shows = [
    {'release_days': '0', 'tvdb_id': 1},
    {'release_days': '1', 'tvdb_id': 2},
    {'release_days': '2', 'tvdb_id': 3},
]
print(suggest_release_day(shows, {}))

# Test 4: all days have 1 show except thu and fri tied at 0 -> with genre tie-break
# thu has a drama show on it... wait thu has 0 shows in this test so no genre there.
shows = [
    {'release_days': '0', 'tvdb_id': 1},
    {'release_days': '1', 'tvdb_id': 2},
    {'release_days': '2', 'tvdb_id': 3},
    {'release_days': '5', 'tvdb_id': 4},
    {'release_days': '6', 'tvdb_id': 5},
]
genre_lookup = {1: ['drama'], 2: ['comedy'], 3: ['drama'], 4: ['horror'], 5: ['comedy']}
# thu(3) and fri(4) are both empty/tied at 0 -> genre check irrelevant since lightest=0 branch used (reason doesn't mention genre)
print(suggest_release_day(shows, genre_lookup, candidate_genres=['drama']))

# Test 5: genre tie-break actually used: wed and thu tied at count=1, wed has drama show, thu has horror show, candidate is drama -> thu should win
shows = [
    {'release_days': '2', 'tvdb_id': 1},  # wed: drama
    {'release_days': '3', 'tvdb_id': 2},  # thu: horror
    {'release_days': '0', 'tvdb_id': 3},  # mon: comedy (lightest tie too, count=1)
]
genre_lookup = {1: ['drama'], 2: ['horror'], 3: ['comedy']}
print(suggest_release_day(shows, genre_lookup, candidate_genres=['drama']))

print("--- test 6: all 7 days occupied, genre tie-break matters ---")
shows = [
    {'release_days': '0', 'tvdb_id': 1},  # mon: comedy, count 2 (with tvdb_id 6 also mon)
    {'release_days': '0', 'tvdb_id': 6},
    {'release_days': '1', 'tvdb_id': 2},  # tue: drama, count 1
    {'release_days': '2', 'tvdb_id': 7},  # wed: drama, count 1
    {'release_days': '3', 'tvdb_id': 3},  # thu: horror, count 1
    {'release_days': '4', 'tvdb_id': 8},  # fri: comedy, count 1
    {'release_days': '5', 'tvdb_id': 9},  # sat: comedy, count 1
    {'release_days': '6', 'tvdb_id': 10}, # sun: comedy, count 1
]
genre_lookup = {1: ['comedy'], 6: ['comedy'], 2: ['drama'], 7: ['drama'], 3: ['horror'], 8: ['comedy'], 9: ['comedy'], 10: ['comedy']}
# tied_days at count=1: tue, wed, thu, fri, sat, sun (all count 1), mon has count 2
# candidate genre = drama -> tue/wed share drama (overlap=1), thu/fri/sat/sun have 0 overlap -> pick thu (first among 0-overlap, lowest index)
print(suggest_release_day(shows, genre_lookup, candidate_genres=['drama']))
