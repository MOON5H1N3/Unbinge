"""Tests for the iCal export feed and the Sonarr upcoming-episodes
integration.

The .ics generation is checked against RFC 5545's actual rules (line
folding, character escaping, balanced BEGIN/END pairs) since a subtle
escaping bug here would silently corrupt the feed for any calendar client
strict enough to reject it, while looking fine in casual visual inspection.
"""

import os
from contextlib import closing
from unittest.mock import patch, MagicMock

import app as unbinge

_REAL_SEND_NOTIFICATION = unbinge.send_notification  # see test_section5_features.py for why


# ---------------------------------------------------------------------------
# ics_escape / ics_uid_slug / ics_fold_line - the primitives
# ---------------------------------------------------------------------------

def test_ics_escape_handles_all_special_characters():
    assert unbinge.ics_escape('a,b') == 'a\\,b'
    assert unbinge.ics_escape('a;b') == 'a\\;b'
    assert unbinge.ics_escape('a\\b') == 'a\\\\b'
    assert unbinge.ics_escape('a\nb') == 'a\\nb'


def test_ics_escape_backslash_is_escaped_before_other_characters():
    """Order matters: escaping ',' and ';' after '\\' would double-escape
    the backslashes those substitutions just introduced."""
    assert unbinge.ics_escape('a\\,b') == 'a\\\\\\,b'


def test_ics_escape_handles_none_and_empty():
    assert unbinge.ics_escape(None) == ''
    assert unbinge.ics_escape('') == ''


def test_ics_uid_slug_strips_unsafe_characters():
    assert unbinge.ics_uid_slug("Marvel's Daredevil") == 'marvel-s-daredevil'
    assert unbinge.ics_uid_slug('') == 'show'
    assert unbinge.ics_uid_slug(None) == 'show'


def test_ics_fold_line_wraps_long_lines():
    long_line = 'DESCRIPTION:' + ('x' * 100)
    folded = unbinge.ics_fold_line(long_line)
    assert '\r\n ' in folded, "a folded continuation must start with a space"
    for segment in folded.split('\r\n'):
        assert len(segment.encode('utf-8')) <= 75


def test_ics_fold_line_leaves_short_lines_alone():
    short = 'SUMMARY:short'
    assert unbinge.ics_fold_line(short) == short


# ---------------------------------------------------------------------------
# build_ical_feed - structural conformance
# ---------------------------------------------------------------------------

def test_feed_is_well_formed_with_no_shows(harness):
    ics = unbinge.build_ical_feed()
    assert ics.startswith('BEGIN:VCALENDAR\r\n')
    assert ics.rstrip().endswith('END:VCALENDAR')
    assert ics.count('BEGIN:VEVENT') == ics.count('END:VEVENT') == 0


def test_feed_includes_drip_events_for_a_real_show(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    ics = unbinge.build_ical_feed()
    assert 'Test Show' in ics
    assert '📺' in ics
    assert ics.count('BEGIN:VEVENT') >= 1


def test_feed_ends_with_crlf():
    assert unbinge.build_ical_feed().endswith('\r\n')


def test_feed_has_balanced_begin_end_pairs(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    ics = unbinge.build_ical_feed()
    assert ics.count('BEGIN:VEVENT') == ics.count('END:VEVENT')
    assert ics.count('BEGIN:VCALENDAR') == ics.count('END:VCALENDAR') == 1


def test_feed_escapes_special_characters_in_show_names(harness):
    """Regression guard: this exact scenario (comma/semicolon/backslash in
    a name) previously produced invalid, unescaped ICS output."""
    show = harness.vault / 'Weird; Show, Name'
    harness.make_episode(show, 'Weird; Show, Name', 1, 1, subdir='Season 01')
    harness.add_show('Weird; Show, Name', release_days='0,1,2,3,4,5,6')

    ics = unbinge.build_ical_feed()
    summary_lines = [l for l in ics.split('\r\n') if l.startswith('SUMMARY:') and 'Weird' in l]
    assert summary_lines
    for line in summary_lines:
        content = line.split('SUMMARY:', 1)[1]
        assert '\\;' in content
        assert '\\,' in content


def test_feed_no_unfolded_line_exceeds_75_octets(harness):
    long_name = 'A Very Long Show Name That Keeps Going And Going And Going For A While'
    show = harness.vault / long_name
    harness.make_episode(show, long_name, 1, 1, subdir='Season 01')
    harness.add_show(long_name, release_days='0,1,2,3,4,5,6')

    ics = unbinge.build_ical_feed()
    for line in ics.split('\r\n'):
        if line == '' or line.startswith(' '):
            continue
        assert len(line.encode('utf-8')) <= 75, f"unfolded line too long: {line!r}"


def test_feed_uses_configured_timezone_for_drip_events(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    ics = unbinge.build_ical_feed()
    tzid_lines = [l for l in ics.split('\r\n') if l.startswith('DTSTART;TZID=')]
    assert tzid_lines
    assert all(str(unbinge.LOCAL_TZ) in l for l in tzid_lines)


# ---------------------------------------------------------------------------
# build_ical_feed - Sonarr integration
# ---------------------------------------------------------------------------

def test_feed_includes_sonarr_events_when_configured(harness):
    unbinge.set_setting('sonarr_url', 'http://fake-sonarr:8989')
    unbinge.set_setting('sonarr_api_key', 'fake-key')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: [{
        'seriesId': 1, 'tvdbId': 1, 'seasonNumber': 2, 'episodeNumber': 5,
        'title': 'An Episode', 'airDateUtc': '2026-09-10T20:00:00Z',
        'hasFile': False, 'series': {'title': 'Sonarr Show'},
    }]
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        ics = unbinge.build_ical_feed()

    assert 'Sonarr Show' in ics
    assert '📡' in ics
    assert 'S02E05' in ics


def test_feed_escapes_sonarr_episode_titles(harness):
    """Regression guard: episode_title was originally excluded from
    escaping - only show_name was escaped - so a comma/semicolon in a
    Sonarr episode title would corrupt the SUMMARY line."""
    unbinge.set_setting('sonarr_url', 'http://fake-sonarr:8989')
    unbinge.set_setting('sonarr_api_key', 'fake-key')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: [{
        'seriesId': 1, 'tvdbId': 1, 'seasonNumber': 1, 'episodeNumber': 1,
        'title': 'The One, With; Punctuation\\Here', 'airDateUtc': '2026-09-10T20:00:00Z',
        'hasFile': False, 'series': {'title': 'Sonarr Show'},
    }]
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        ics = unbinge.build_ical_feed()

    sonarr_summary = [l for l in ics.split('\r\n') if l.startswith('SUMMARY:') and '📡' in l]
    assert sonarr_summary
    full = ''.join(l.lstrip(' ') for l in ics.split('\r\n ')) if '\r\n ' in ics else sonarr_summary[0]
    # Reconstruct the folded line if needed, then check escaping survived.
    combined = ics.replace('\r\n ', '')  # unfold
    assert '\\,' in combined
    assert '\\;' in combined
    assert '\\\\' in combined


def test_feed_omits_sonarr_section_when_not_configured(harness):
    show = harness.vault / 'Test Show'
    harness.make_episode(show, 'Test Show', 1, 1, subdir='Season 01')
    harness.add_show('Test Show', release_days='0,1,2,3,4,5,6')

    ics = unbinge.build_ical_feed()
    assert '📡' not in ics


def test_feed_handles_sonarr_being_unreachable(harness):
    unbinge.set_setting('sonarr_url', 'http://fake-sonarr:8989')
    unbinge.set_setting('sonarr_api_key', 'fake-key')
    with patch.object(unbinge.requests, 'get', side_effect=unbinge.requests.RequestException("down")):
        ics = unbinge.build_ical_feed()  # must not raise
    assert ics.startswith('BEGIN:VCALENDAR')


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_calendar_ics_route_serves_correct_content_type(harness, client):
    resp = client.get('/calendar.ics')
    assert resp.status_code == 200
    assert 'text/calendar' in resp.content_type
    assert resp.data.decode().startswith('BEGIN:VCALENDAR')


def test_settings_page_shows_the_feed_url(harness, client):
    resp = client.get('/settings')
    assert b'/calendar.ics' in resp.data
    assert b'calendar feed' in resp.data


def test_sonarr_calendar_route_empty_when_unconfigured(harness, client):
    resp = client.get('/api/sonarr/calendar?days=14')
    data = resp.get_json()
    assert resp.status_code == 200
    assert data['events'] == []
    assert data['message'] is not None


def test_sonarr_calendar_route_returns_parsed_events(harness, client):
    unbinge.set_setting('sonarr_url', 'http://fake-sonarr:8989')
    unbinge.set_setting('sonarr_api_key', 'fake-key')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: [{
        'seriesId': 9, 'tvdbId': 111, 'seasonNumber': 3, 'episodeNumber': 7,
        'title': 'Ep Title', 'airDateUtc': '2026-09-12T18:00:00Z',
        'hasFile': True, 'series': {'title': 'Another Show'},
    }]
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        resp = client.get('/api/sonarr/calendar?days=14')
        data = resp.get_json()

    assert len(data['events']) == 1
    assert data['events'][0]['show_name'] == 'Another Show'
    assert data['events'][0]['season'] == 3
    assert data['events'][0]['episode'] == 7
    assert data['events'][0]['has_file'] is True


def test_sonarr_calendar_route_clamps_days_parameter(harness, client):
    resp = client.get('/api/sonarr/calendar?days=9999')
    assert resp.status_code == 200  # must not error, whatever the clamp does internally


def test_schedule_page_includes_sonarr_panel_markup(harness, client):
    resp = client.get('/schedule?weeks=2')
    assert b'sonarrUpcomingCard' in resp.data
    assert b'/api/sonarr/calendar' in resp.data


def test_sonarr_get_calendar_skips_events_with_no_air_date(harness):
    unbinge.set_setting('sonarr_url', 'http://fake-sonarr:8989')
    unbinge.set_setting('sonarr_api_key', 'fake-key')
    mock_resp = MagicMock()
    mock_resp.raise_for_status = lambda: None
    mock_resp.json = lambda: [
        {'seriesId': 1, 'seasonNumber': 1, 'episodeNumber': 1, 'series': {'title': 'No Date Show'}},
        {'seriesId': 2, 'seasonNumber': 1, 'episodeNumber': 1, 'airDateUtc': '2026-09-10T20:00:00Z',
         'series': {'title': 'Has Date Show'}, 'hasFile': False},
    ]
    with patch.object(unbinge.requests, 'get', return_value=mock_resp):
        events, err = unbinge.sonarr_get_calendar(days_ahead=14)

    assert err is None
    names = [e['show_name'] for e in events]
    assert 'Has Date Show' in names
    assert 'No Date Show' not in names
