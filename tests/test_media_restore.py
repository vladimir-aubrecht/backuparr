import copy
import json
import zipfile

import pytest
import requests

from apps.media_restore import MediaRestore
from apps.media_server import MediaServerError
from test_media_server_export import driver, media, USERS


def target():
    app = driver()
    app.session.routes['/Users'] = [dict(copy.deepcopy(u), Id='new-' + u['Id'], HasPassword=True) for u in USERS]
    app._items = lambda *a, **kw: iter([dict(media(i), Id='new-' + i) for i in ('one', 'two', 'three')])
    writes = []
    def request(method, url, **kwargs):
        writes.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 204
        response._content = b''
        response._content_consumed = True
        return response
    app.session.request = request
    return app, writes


def test_preview_is_read_only_and_restores_each_user_to_new_item_ids(tmp_path):
    archive = driver().backup(tmp_path)
    app, writes = target()
    with zipfile.ZipFile(archive) as zf:
        restore = MediaRestore(app, zf)
        counts = restore.plan()
        assert not writes
        assert counts['watch states'] == 6
        assert restore.plan() == counts  # Re-preview must not duplicate destructive operations.
        restore.apply()
    state = {url: kw['json'] for _, url, kw in writes if url.endswith('/UserData')}
    prefix = 'http://media:8096/base/Users/'
    assert state[prefix + 'new-admin-id/Items/new-one/UserData']['Played'] is True
    hidden = state[prefix + 'new-hidden-id/Items/new-one/UserData']
    assert hidden['Played'] is False and hidden['PlayCount'] == hidden['PlaybackPositionTicks'] == 0
    assert state[prefix + 'new-hidden-id/Items/new-two/UserData']['PlaybackPositionTicks'] == 50000
    assert all(not kw['allow_redirects'] for _, _, kw in writes)
    with pytest.raises(MediaServerError, match='preview'):
        restore.apply()


@pytest.mark.parametrize('failure', ['user', 'media', 'plugin', 'password', 'major-version'])
def test_failed_preflight_never_writes_or_allows_partial_plan(tmp_path, failure):
    source = driver()
    source.session.routes['/Users'] = [dict(copy.deepcopy(u), HasPassword=True) for u in USERS]
    archive = source.backup(tmp_path)
    app, writes = target()
    if failure == 'user':
        app.session.routes['/Users'][0]['Name'] = 'Someone else'
    elif failure == 'media':
        app._items = lambda *a, **kw: iter([])
    elif failure == 'plugin':
        app.session.routes['/Plugins'] = []
    elif failure == 'password':
        app.session.routes['/Users'][0]['HasPassword'] = False
    else:
        app.session.routes['/System/Info']['Version'] = '11.0.0'
    with zipfile.ZipFile(archive) as zf:
        restore = MediaRestore(app, zf)
        with pytest.raises(MediaServerError):
            restore.plan()
        with pytest.raises(MediaServerError, match='preview'):
            restore.apply()
    assert not writes


def test_ambiguous_provider_identity_is_not_silently_assigned(tmp_path):
    app, writes = target()
    with zipfile.ZipFile(driver().backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf)
        with pytest.raises(MediaServerError, match='uniquely'):
            restore._match_items([media('one')], [dict(media('one'), Id='a'), dict(media('one'), Id='b')])
    assert not writes


def test_path_mapping_and_id_mapping_do_not_change_zero_and_false(tmp_path):
    app, _ = target()
    with zipfile.ZipFile(driver().backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf, [('/old', '/new')])
        restore._match_items([dict(media('one'), Path='/old/a.mkv', ProviderIds={})],
                             [dict(media('one'), Id='new-id', Path='/new/a.mkv', ProviderIds={})])
        assert restore.item_map == {'one': 'new-id'}
        assert restore._translate({'p': '/old/a.mkv', 'sibling': '/older/b', 'zero': 0, 'false': False}) == {
            'p': '/new/a.mkv', 'sibling': '/older/b', 'zero': 0, 'false': False}


@pytest.mark.parametrize('error', [requests.ConnectionError('fixture-secret'), 302, 403, 500])
def test_restore_stops_and_redacts_transport_or_server_errors(tmp_path, error):
    app, writes = target()
    def failure(*a, **kw):
        if isinstance(error, Exception):
            raise error
        response = requests.Response()
        response.status_code = error
        response._content = b'fixture-secret'
        response._content_consumed = True
        return response
    app.session.request = failure
    with zipfile.ZipFile(driver().backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf)
        restore.plan()
        with pytest.raises(MediaServerError) as exc:
            restore.apply()
        assert 'fixture-secret' not in str(exc.value)


def test_metadata_restore_supplies_required_arrays_and_uses_target_id(tmp_path):
    app, _ = target()
    with zipfile.ZipFile(driver().backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf)
        restore.plan()
        data = next(kw['json'] for kind, _, _, kw in restore.actions if kind == 'metadata')
        assert data['Id'] == 'new-one'
        assert data['Tags'] == [] and data['Genres'] == []
        assert data['ProviderIds'] == {'Tmdb': 'one'}


def test_playlist_restore_remaps_members_and_preserves_order_and_repetition(tmp_path):
    source = driver()
    playlist = {'Id': 'playlist-id', 'Type': 'Playlist', 'Name': 'Fixture playlist'}
    source._items = lambda *a, **kw: iter(
        [dict(media(i), PlaylistItemId='entry-' + str(n)) for n, i in enumerate(('two', 'one', 'two'))]
        if kw.get('playlist') else [media(i) for i in ('one', 'two', 'three')] + [playlist])
    app, writes = target()
    app._items = lambda *a, **kw: iter(
        [dict(media('new-three'), PlaylistItemId='target-entry')]
        if kw.get('playlist') else [dict(media(i), Id='new-' + i) for i in ('one', 'two', 'three')]
        + [dict(playlist, Id='new-playlist-id')])
    with zipfile.ZipFile(source.backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf)
        restore.plan()
        restore.apply()
    playlist_writes = [(method, url, kwargs) for method, url, kwargs in writes if '/Playlists/' in url]
    assert playlist_writes[0][0] == 'DELETE'
    assert playlist_writes[0][2]['params']['EntryIds'] == 'target-entry'
    assert [kw['params']['Ids'] for method, _, kw in playlist_writes if method == 'POST'] == ['new-two', 'new-one', 'new-two']
    assert all('/Playlists/new-playlist-id/Items' in url for _, url, _ in playlist_writes)


@pytest.mark.parametrize('server,metadata,expected', [
    ('emby', {'TagItems': [{'Name': 'Family', 'Id': 123}, {'Name': 'České', 'Id': 456}]},
     {'TagItems': [{'Name': 'Family'}, {'Name': 'České'}]}),
    ('emby', {'TagItems': []}, {'TagItems': []}),
    ('emby', {'Tags': ['Legacy']}, {'TagItems': [{'Name': 'Legacy'}]}),
    ('jellyfin', {'Tags': ['Family', 'České']}, {'Tags': ['Family', 'České']}),
])
def test_restore_uses_server_tag_format_without_source_tag_ids(tmp_path, server, metadata, expected):
    source = driver()
    source.name = server
    source._items = lambda *a, **kw: iter([dict(media('one'), **metadata)])
    app, writes = target()
    app.name = server
    with zipfile.ZipFile(source.backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf)
        restore.plan()
        restore.apply()
    body = next(kw['json'] for method, url, kw in writes if url.endswith('/Items/new-one'))
    assert {key: body[key] for key in ('Tags', 'TagItems') if key in body} == expected


def membership_fixture(kind='Playlist', owner='z-admin', omit_owner=False, incomplete_owner=False):
    source = driver()
    users = [dict(copy.deepcopy(USERS[0]), Id='z-admin'), dict(copy.deepcopy(USERS[1]), Id='a-viewer')]
    source.session.routes['/Users'] = users
    container = {'Id': 'list-id', 'Type': kind, 'Name': 'Shared list'}
    full = ('two', 'one', 'two') if kind == 'Playlist' else ('two', 'one')

    def source_items(user_id=None, **kw):
        if kw.get('playlist') or kw.get('ParentId'):
            complete = user_id == owner
            if incomplete_owner:
                complete = not complete
            ids = full if complete else ('one',)
            return iter([dict(media(item), PlaylistItemId=f'entry-{i}') for i, item in enumerate(ids)])
        include = not (omit_owner and user_id == owner)
        return iter([media(i) for i in ('one', 'two')] + ([container] if include else []))

    source._items = source_items
    app, writes = target()
    app.session.routes['/Users'] = [dict(u, Id='new-' + u['Id']) for u in users]
    app._items = lambda *a, **kw: iter(
        [dict(media('three'), Id='new-three', PlaylistItemId='old-target-entry')]
        if kw.get('playlist') or kw.get('ParentId') else
        [dict(media(i), Id='new-' + i) for i in ('one', 'two')] + [dict(container, Id='new-list-id')])
    return source, app, writes, full


@pytest.mark.parametrize('kind,owner', [('Playlist', 'z-admin'), ('Playlist', 'a-viewer'), ('BoxSet', 'z-admin')])
def test_membership_restore_uses_owner_or_admin_export_not_first_uuid(tmp_path, kind, owner):
    source, app, writes, full = membership_fixture(kind, owner)
    owners = {'list-id': 'Hidden'} if owner == 'a-viewer' else None
    with zipfile.ZipFile(source.backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf, playlist_owners=owners)
        restore.plan()
        restore.apply()
    prefix = '/Playlists/' if kind == 'Playlist' else '/Collections/'
    updates = [(method, kw) for method, url, kw in writes if prefix in url]
    assert [method for method, _ in updates].count('DELETE') == 1
    restored = [item for method, kw in updates if method == 'POST' for item in kw['params']['Ids'].split(',')]
    assert restored == ['new-' + item for item in full]
    if kind == 'Playlist':
        assert all(kw['params']['UserId'] == 'new-' + owner for _, kw in updates)


@pytest.mark.parametrize('failure', ['missing-owner', 'incomplete-owner', 'incomplete-explicit-owner'])
def test_missing_or_incomplete_playlist_owner_export_stops_before_any_write(tmp_path, failure):
    explicit = failure == 'incomplete-explicit-owner'
    source, app, writes, _ = membership_fixture(
        owner='a-viewer' if explicit else 'z-admin',
        omit_owner=failure == 'missing-owner', incomplete_owner=failure != 'missing-owner')
    with zipfile.ZipFile(source.backup(tmp_path)) as zf:
        restore = MediaRestore(app, zf, playlist_owners={'list-id': 'Hidden'} if explicit else None)
        with pytest.raises(MediaServerError, match='membership export.*selected user'):
            restore.plan()
        with pytest.raises(MediaServerError, match='preview'):
            restore.apply()
    assert writes == []


@pytest.mark.parametrize('server', ['emby', 'jellyfin'])
def test_folder_deletion_grants_follow_library_ids_on_another_server(tmp_path, server):
    source = driver()
    source.name = server
    source.session.routes['/Users'] = copy.deepcopy(USERS)
    source.session.routes['/Users'][1]['Policy'].update({
        'EnableContentDeletion': False, 'EnableContentDeletionFromFolders': ['10'],
        'EnabledFolders': ['10', '20'], 'EnableAllFolders': False,
    })
    folders = [
        {'Name': 'Temporary', 'ItemId': '10', 'LibraryOptions': {}},
        {'Name': 'Family archive', 'ItemId': '20', 'LibraryOptions': {}},
    ]
    source.session.routes['/Library/VirtualFolders'] = folders
    app, writes = target()
    app.name = server
    app.session.routes['/Library/VirtualFolders'] = [dict(f, ItemId={'10': '20', '20': '10'}[f['ItemId']]) for f in folders]
    with zipfile.ZipFile(source.backup(tmp_path)) as archive:
        restore = MediaRestore(app, archive)
        restore.plan()
        body = next(kw['json'] for section, _, path, kw in restore.actions
                    if section == 'user policies' and 'new-hidden-id' in path)
    assert body['EnabledFolders'] == ['20', '10']
    assert body['EnableContentDeletionFromFolders'] == ['20']
    assert not writes


@pytest.mark.parametrize('server', ['emby', 'jellyfin'])
def test_restore_preserves_editable_composers_and_date_added(tmp_path, server):
    source = driver()
    source.name = server
    audio = dict(media('one'), Type='Audio', Composers=['Fixture Composer'], DateCreated='2024-02-03T12:34:56Z')
    source._items = lambda *a, **kw: iter([audio])
    app, writes = target()
    app.name = server
    app._items = lambda *a, **kw: iter([dict(audio, Id='new-one', Composers=[], DateCreated='2026-10-17T00:00:00Z')])
    with zipfile.ZipFile(source.backup(tmp_path)) as archive:
        restore = MediaRestore(app, archive)
        restore.plan()
        restore.apply()
    body = next(kw['json'] for _, url, kw in writes if url.endswith('/Items/new-one'))
    assert body['DateCreated'] == audio['DateCreated']
    if server == 'emby':
        assert body['Composers'] == audio['Composers']
