import copy
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

import pytest
import requests

from apps.media_server import MediaServerApp, MediaServerError


USERS = [
    {'Id': 'admin-id', 'Name': 'Admin', 'Policy': {'IsAdministrator': True}, 'Configuration': {}},
    {'Id': 'hidden-id', 'Name': 'Hidden', 'Policy': {'IsHidden': True, 'IsDisabled': True}, 'Configuration': {'AudioLanguagePreference': 'cs'}},
]


def media(item_id, played=False, ticks=0):
    return {'Id': item_id, 'Type': 'Movie', 'Name': item_id, 'Path': f'/media/{item_id}.mkv',
            'ProviderIds': {'Tmdb': item_id}, 'UserData': {
                'Played': played, 'PlaybackPositionTicks': ticks, 'PlayCount': int(played),
                'IsFavorite': played, 'LastPlayedDate': '2026-10-06T12:00:00Z' if played else None,
            }}


class Session:
    def __init__(self, routes=None, pages=None, status=200, error=None):
        self.routes = {
            '/System/Info': {'Id': 'server-id', 'Version': '10.11.0'},
            '/System/Configuration': {'ServerName': 'Fixture', 'UICulture': 'cs-CZ'},
            '/Users': USERS,
            '/Plugins': [{'Id': 'plugin-id', 'Name': 'Fixture Plugin', 'ConfigurationFileName': 'fixture.xml'}],
            '/Plugins/plugin-id/Configuration': {'ProviderToken': 'fixture-provider-token'},
            '/ScheduledTasks': [{'Id': 'task-id', 'Key': 'fixture-task', 'Triggers': [{'Type': 'DailyTrigger'}]}],
            '/Auth/Keys': {'Items': [{'AccessToken': 'fixture-saved-api-key'}], 'TotalRecordCount': 1},
            '/Devices': {'Items': [], 'TotalRecordCount': 0},
            '/LiveTv/Timers': {'Items': [{'Id': 'timer-id'}], 'TotalRecordCount': 1},
            '/LiveTv/SeriesTimers': {'Items': [], 'TotalRecordCount': 0},
            '/DisplayPreferences/usersettings': {'CustomPrefs': {'theme': 'dark'}},
            '/Library/VirtualFolders': [{'Name': 'Movies', 'ItemId': 'library-id', 'Locations': ['/media'], 'LibraryOptions': {'EnableRealtimeMonitor': True}}],
        }
        self.routes.update({f'/System/Configuration/{key}': {'FixtureOption': True} for key in MediaServerApp.configuration_keys})
        self.routes.update(routes or {})
        self.pages = pages
        self.status, self.error = status, error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        path = urlsplit(url).path.removeprefix('/base')
        if path == '/Items':
            if self.pages is not None and kwargs['params'].get('UserId'):
                value = self.pages.pop(0)
                if isinstance(value, Exception):
                    raise value
            else:
                p = kwargs['params']
                data = [media('one', p.get('UserId') == 'admin-id'), media('two', False, 50000), media('three')]
                start, limit = p['StartIndex'], p['Limit']
                value = {'TotalRecordCount': len(data), 'Items': data[start:start+limit]}
        else:
            value = self.routes[path]
        response = requests.Response()
        response.status_code = self.status
        response._content = json.dumps(value).encode()
        response._content_consumed = True
        return response


def driver(**kwargs):
    app = MediaServerApp('http://media:8096/base/', 'fixture-private-key')
    app.page_size = 2
    app.session = Session(**kwargs)
    return app


def test_api_export_keeps_each_users_watched_unwatched_progress_and_configuration(tmp_path):
    app = driver()
    path = Path(app.backup(str(tmp_path)))
    assert path.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        users = json.loads(zf.read('users.json'))
        assert users == USERS
        admin = [json.loads(line) for line in zf.read('user-items/admin-id.jsonl').splitlines()]
        hidden = [json.loads(line) for line in zf.read('user-items/hidden-id.jsonl').splitlines()]
        assert len(admin) == len(hidden) == 3
        assert admin[0]['UserData']['Played'] is True
        assert hidden[0]['UserData']['Played'] is False
        assert hidden[0]['UserData']['PlaybackPositionTicks'] == 0
        assert hidden[1]['UserData']['PlaybackPositionTicks'] == 50000
        assert hidden[1]['ProviderIds'] and hidden[1]['Path']
        manifest = json.loads(zf.read('manifest.json'))
        assert manifest['user_item_counts'] == {'admin-id': 3, 'hidden-id': 3}
        assert manifest['format_version'] == 1 and manifest['server_id'] == 'server-id'
        assert manifest['limitations']
        assert b'cannot be imported' in zf.read('RESTORE.txt')
        assert json.loads(zf.read('plugin-configuration/plugin-id.json'))['ProviderToken'] == 'fixture-provider-token'
        assert 'api-keys.json' not in zf.namelist()
        assert 'devices.json' not in zf.namelist()
        assert json.loads(zf.read('scheduled-tasks.json'))[0]['Triggers']
        assert json.loads(zf.read('live-tv/timers.json'))['Items']
        assert len(zf.read('catalog.jsonl').splitlines()) == 3
        assert json.loads(zf.read('configuration/encoding.json')) == {'FixtureOption': True}
        assert json.loads(zf.read('system-configuration.json'))['UICulture'] == 'cs-CZ'
        assert json.loads(zf.read('libraries.json'))[0]['LibraryOptions']['EnableRealtimeMonitor']
    item_calls = [kw['params'] for url, kw in app.session.calls if url.endswith('/Items') and kw['params'].get('UserId')]
    assert [(p['UserId'], p['StartIndex']) for p in item_calls] == [
        ('admin-id', 0), ('admin-id', 2), ('hidden-id', 0), ('hidden-id', 2)]
    assert all(p['EnableUserData'] == 'true' and p['Recursive'] == 'true' for p in item_calls)
    assert all(p['CollapseBoxSetItems'] == 'false' and p['GroupItemsIntoCollections'] == 'false' for p in item_calls)
    assert all('IsPlayed' not in p and 'Filters' not in p for p in item_calls)
    assert all(not kw['allow_redirects'] for _, kw in app.session.calls)
    assert 'fixture-private-key' not in str(app.session.calls)


def test_test_connection_does_not_create_or_trigger_any_backup():
    app = driver()
    assert 'limited API export' in app.test_connection()
    assert [urlsplit(url).path for url, _ in app.session.calls] == [
        '/base/System/Info', '/base/System/Configuration', '/base/Users']


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 404, 500])
def test_http_failure_is_never_a_successful_backup(tmp_path, status):
    with pytest.raises(MediaServerError):
        driver(status=status).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('pages', [
    [{'TotalRecordCount': 1, 'Items': []}],
    [{'TotalRecordCount': 0, 'Items': [media('one')]}],
    [{'TotalRecordCount': '1', 'Items': [media('one')]}],
    [{'Items': [media('one')]}],
    [{'TotalRecordCount': 2, 'Items': [media('one'), media('one')]}],
    [{'TotalRecordCount': 2, 'Items': [media('one')]}, {'TotalRecordCount': 3, 'Items': [media('two')]}],
    [{'TotalRecordCount': 2, 'Items': [media('one')]}, {'TotalRecordCount': 2, 'Items': [media('one')]}],
    [{'TotalRecordCount': 1, 'Items': [{'Id': 'one', 'Type': 'Movie'}]}],
    [{'TotalRecordCount': 1, 'Items': [{'Id': 'one', 'Type': 'Audio', 'UserData': {'Played': None}}]}],
    [{'TotalRecordCount': 1, 'Items': [None]}],
    [{'TotalRecordCount': 1, 'Items': {'Id': 'one'}}],
])
def test_missing_state_and_broken_pagination_remove_partial_archive(tmp_path, pages):
    with pytest.raises(MediaServerError):
        driver(pages=copy.deepcopy(pages)).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_interrupted_later_page_is_sanitized_and_partial_archive_removed(tmp_path):
    app = driver(pages=[{'TotalRecordCount': 2, 'Items': [media('one')]}, requests.ConnectionError('fixture-private-key')])
    with pytest.raises(MediaServerError) as exc:
        app.backup(str(tmp_path))
    assert 'fixture-private-key' not in str(exc.value)
    assert exc.value.__suppress_context__
    assert not list(tmp_path.iterdir())


def test_empty_libraries_have_explicit_zero_counts(tmp_path):
    empty = {'TotalRecordCount': 0, 'Items': []}
    path = driver(pages=[empty, empty]).backup(str(tmp_path))
    with zipfile.ZipFile(path) as zf:
        assert zf.read('user-items/admin-id.jsonl') == b''
        assert json.loads(zf.read('manifest.json'))['user_item_counts'] == {'admin-id': 0, 'hidden-id': 0}


@pytest.mark.parametrize('users', [[], {}, [None], [{'Id': '../../outside'}], USERS + USERS, [{'Id': 'one', 'Policy': {}}]])
def test_invalid_users_never_become_filenames(tmp_path, users):
    with pytest.raises(MediaServerError):
        driver(routes={'/Users': users}).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('url', [None, 'ftp://host', 'http://user:pass@host', 'http://host?api_key=secret', 'http://host:invalid'])
def test_invalid_urls_fail_before_requests(url):
    with pytest.raises(MediaServerError):
        MediaServerApp(url, 'fixture-key')


def test_real_http_headers_routes_and_json_export(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    received = []
    data = Session().routes | {'/Items': {'TotalRecordCount': 1, 'Items': [media('one')]}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get('X-Emby-Token')))
            path = urlsplit(self.path).path.removeprefix('/base')
            assert path in data
            body = json.dumps(data[path]).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app = MediaServerApp(f'http://127.0.0.1:{server.server_port}/base', 'fixture-private-key')
        app.session.trust_env = False  # Local fixture must not use a session's egress proxy.
        with zipfile.ZipFile(app.backup(str(tmp_path))) as zf:
            assert json.loads(zf.read('manifest.json'))['user_item_counts'] == {'admin-id': 1, 'hidden-id': 1}
        assert all(token == 'fixture-private-key' and 'fixture-private-key' not in path for path, token in received)
        assert any(path.startswith('/System/Configuration/') for path, _ in [(p.removeprefix('/base'), t) for p, t in received])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_playlists_keep_order_and_duplicate_tracks_with_distinct_entry_ids():
    app = driver(pages=[{'TotalRecordCount': 2, 'Items': [
        dict(media('one'), PlaylistItemId='entry-1'), dict(media('one'), PlaylistItemId='entry-2')]}])
    # Use the fake /Items route to verify the shared playlist pagination behaviour.
    rows = list(app._items('admin-id', playlist=True))
    assert [r['PlaylistItemId'] for r in rows] == ['entry-1', 'entry-2']
    assert 'SortBy' not in app.session.calls[0][1]['params']


def test_emby_style_plugins_without_configuration_are_recorded_without_broken_api_calls(tmp_path):
    app = driver(routes={'/Plugins': [{'Id': 'no-config', 'Name': 'Provider without configuration file'}]})
    app.plugin_configuration_filename_required = True
    with zipfile.ZipFile(app.backup(str(tmp_path))) as z:
        missing = json.loads(z.read('manifest.json'))['unavailable']
        assert missing[0]['endpoint'] == '/Plugins/no-config/Configuration'
    assert not any('/Plugins/no-config/Configuration' in url for url, _ in app.session.calls)


def test_unavailable_optional_api_is_recorded_but_auth_errors_are_fatal():
    app = driver(status=404)
    missing = []
    assert app._get('/Devices', dict, unavailable=missing) is None
    assert missing == [{'endpoint': '/Devices', 'status': 404, 'reason': 'Not exposed by this server/version/plugin'}]
    for status in (401, 403, 500):
        with pytest.raises(MediaServerError):
            driver(status=status)._get('/Devices', dict, unavailable=[])


def test_artwork_is_streamed_and_interrupted_download_fails_without_token_leak(tmp_path, monkeypatch):
    app = driver()
    image = b'\x89PNG\r\n\x1a\nfixture'
    response = requests.Response()
    response.status_code = 200
    response._content = image
    response._content_consumed = True
    response.headers['Content-Type'] = 'image/png'
    monkeypatch.setattr(app.session, 'get', lambda *args, **kwargs: response)
    with zipfile.ZipFile(tmp_path / 'image.zip', 'w') as z:
        assert app._image(z, '/Items/one/Images/Primary/0', 'images/one/Primary-0') == 'image/png'
    with zipfile.ZipFile(tmp_path / 'image.zip') as z:
        assert z.read('images/one/Primary-0') == image
    def broken(**kwargs):
        yield b'partial'
        raise requests.ConnectionError('fixture-private-key')
    monkeypatch.setattr(response, 'iter_content', lambda *args, **kwargs: broken())
    with zipfile.ZipFile(tmp_path / 'broken.zip', 'w') as z, pytest.raises(MediaServerError) as exc:
        app._image(z, '/Items/one/Images/Primary/0', 'image')
    assert 'fixture-private-key' not in str(exc.value)


def test_collection_membership_exported_even_if_user_listings_hide_collections(tmp_path):
    app = driver()
    collection = {'Id': 'collection-id', 'Name': 'Collection', 'Type': 'BoxSet'}
    calls = []
    def items(user_id=None, **kwargs):
        calls.append((user_id, kwargs))
        if kwargs.get('ParentId'):
            return iter([media('one')])
        return iter([media('one')] if user_id else [media('one'), collection])
    app._items = items
    with zipfile.ZipFile(app.backup(tmp_path)) as zf:
        rows = zf.read('memberships/admin-id/collection-id.jsonl').splitlines()
        assert json.loads(rows[0])['Id'] == 'one'
    assert ('admin-id', {'ParentId': 'collection-id', 'Recursive': 'false'}) in calls


def test_server_that_cannot_restore_repeated_playlist_items_refuses_them():
    app = driver(pages=[{'TotalRecordCount': 2, 'Items': [media('one'), media('one')]}])
    app.playlist_duplicates_restorable = False
    with pytest.raises(MediaServerError, match='cannot restore repeated'):
        list(app._items('admin-id', playlist=True))
