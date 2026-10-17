import copy
import json
import os
import zipfile
from urllib.parse import urlsplit

import pytest
import requests

from apps.open_webui import FORMAT, OpenWebUIApp, OpenWebUIError
from apps.open_webui_restore import OpenWebUIRestore

KEY = 'fixture-secret-key'
ADMIN = {'id': 'admin-old', 'email': 'admin@example.com', 'name': 'Admin', 'role': 'admin', 'settings': {'ui': {'theme': 'dark'}}}
ALICE = {'id': 'alice-old', 'email': 'alice@example.com', 'name': 'Alice', 'role': 'user', 'settings': {'ui': {'theme': 'light'}}}


class Reply:
    def __init__(self, data=None, status=200, raw=None):
        self.data, self.status_code, self.raw = data, status, raw
        self.closed = False
        self.text = json.dumps(data) if raw is None else raw.decode()

    def json(self):
        return copy.deepcopy(self.data)

    def close(self):
        self.closed = True

    def iter_content(self, size):
        yield self.raw


def page(rows, key='items'):
    return {key: rows, 'total': len(rows)}


class API:
    def __init__(self, target=False):
        self.headers, self.calls, self.writes = {}, [], []
        self.target = target
        self.model = None
        self.routes = {
            '/api/v1/auths/': dict(ALICE, id='alice-new') if target else ADMIN,
            '/api/version': {'version': '0.12.3'},
            '/api/v1/configs/export': {'ui.name': 'My WebUI', 'openai.key': 'provider-private-key'},
            '/api/v1/users/': page([ADMIN, ALICE], 'users'),
            '/api/v1/chats/all': [],
            '/api/v1/chats/all/db': [
                {'id': 'chat-a', 'user_id': 'alice-old', 'chat': {'title': 'Alice secret', 'history': {'messages': {
                    'm': {'user_id': 'alice-old', 'content': 'hello', 'files': [{'id': 'file-a', 'url': '/api/v1/files/file-a/content'}]}}}},
                 'variables': {'answer': '42'}, 'archived': True, 'pinned': True, 'meta': {'tags': ['important']}, 'folder_id': 'old-folder'},
                {'id': 'chat-admin', 'user_id': 'admin-old', 'chat': {'title': 'Admin private'}}],
            '/api/v1/models/export': [] if target else [{'id': 'my-model', 'user_id': 'alice-old', 'name': 'Preset', 'base_model_id': 'provider-model', 'params': {'temperature': 0.2}, 'meta': {'knowledge': [{'id': 'knowledge-a', 'type': 'collection'}]}, 'access_grants': [{'principal_id': 'old-group'}]}],
            '/api/v1/prompts/list': page([] if target else [{'id': 'prompt-a', 'command': '/test', 'name': 'Test', 'content': 'text', 'user_id': 'alice-old', 'is_active': False}]),
            '/api/v1/tools/export': [] if target else [{'id': 'tool-a', 'name': 'Tool', 'content': 'class Tools: pass', 'meta': {}, 'user_id': 'alice-old'}],
            '/api/v1/tools/id/tool-a/valves': {'secret': 'tool-private'},
            '/api/v1/functions/export': [] if target else [{'id': 'function-a', 'user_id': 'admin-old', 'name': 'Function', 'content': 'class Filter: pass', 'meta': {}, 'type': 'filter', 'is_active': True, 'is_global': True}],
            '/api/v1/functions/id/function-a/valves': {'secret': 'function-private'},
            '/api/v1/files/': page([] if target else [{'id': 'file-a', 'user_id': 'alice-old', 'filename': 'note.txt', 'meta': {'name': 'note.txt', 'size': 4, 'content_type': 'text/plain'}, 'data': {'content': 'edited text'}}]),
            '/api/v1/knowledge/': page([] if target else [{'id': 'knowledge-a', 'user_id': 'alice-old', 'name': 'Docs', 'description': 'References', 'meta': {}}]),
            '/api/v1/knowledge/knowledge-a/files': page([{'id': 'file-a'}]),
        }

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlsplit(url).path.removeprefix('/base')
        assert kwargs['allow_redirects'] is False
        if method == 'GET':
            if path == '/api/v1/models/model':
                return Reply(self.model, status=200 if self.model else 404)
            if path == '/api/v1/files/file-a/content':
                return Reply(raw=b'data')
            value = self.routes[path]
            if isinstance(value, Reply):
                return value
            if isinstance(value, Exception):
                raise value
            return Reply(value)
        self.writes.append((path, kwargs))
        if path == '/api/v1/files/':
            assert 'files' not in kwargs
            payload = b''.join(iter(lambda: kwargs['data'].read(17), b''))
            assert b'\r\ndata\r\n' in payload
            assert kwargs['headers']['Content-Type'].startswith('multipart/form-data; boundary=')
            return Reply({'id': 'file-new'})
        if path == '/api/v1/users/user/settings/update':
            return Reply(kwargs['json'])
        if path.endswith('/data/content/update'):
            return Reply(kwargs['json'])
        if path == '/api/v1/knowledge/create':
            return Reply({'id': 'knowledge-new'})
        if path == '/api/v1/tools/create':
            return Reply(kwargs['json'])
        if path == '/api/v1/models/import':
            self.model = dict(kwargs['json']['models'][0], user_id='alice-new')
            return Reply(True)
        if path == '/api/v1/prompts/create':
            return Reply({'id': 'prompt-new'})
        if path == '/api/v1/chats/import':
            return Reply([{'id': 'chat-new', 'user_id': 'alice-new'}])
        if path == '/api/v1/functions/sync':
            return Reply(kwargs['json']['functions'])
        return Reply({})


def app(target=False):
    instance = OpenWebUIApp('http://server:8080/base/', KEY)
    api = API(target)
    api.headers = instance.session.headers
    instance.session = api
    return instance, api


@pytest.fixture
def exported(tmp_path):
    source, api = app()
    return source.backup(tmp_path), api


def test_export_is_private_complete_and_authenticates_without_query_keys(exported):
    path, api = exported
    assert os.stat(path).st_mode & 0o777 == 0o600
    with zipfile.ZipFile(path) as z:
        assert z.read('files/file-a') == b'data'
        assert z.getinfo('files/file-a').extract_version >= 45  # Streamed members allow ZIP64 without knowing size.
        assert len(json.loads(z.read('chats.json'))) == 2
        assert json.loads(z.read('manifest.json'))['format'] == FORMAT
        assert json.loads(z.read('tools.json'))[0]['valves']['secret'] == 'tool-private'
        assert 'password' not in json.loads(z.read('users.json'))[0]
        assert 'not a complete server/database snapshot' in z.read('RECOVERY.txt').decode()
    assert api.headers['Authorization'] == f'Bearer {KEY}'
    assert all(KEY not in url and kwargs['allow_redirects'] is False for _, url, kwargs in api.calls)
    assert all('/db/download' not in url for _, url, _ in api.calls)


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 500])
def test_redirects_and_http_errors_are_closed_and_redacted(status):
    source, api = app()
    reply = Reply({'detail': KEY}, status=status)
    api.routes['/api/v1/auths/'] = reply
    with pytest.raises(OpenWebUIError) as caught:
        source.test_connection()
    assert KEY not in str(caught.value)
    assert reply.closed
    assert len(api.calls) == 1


def test_network_exception_does_not_echo_request_or_key():
    source, api = app()
    api.routes['/api/v1/auths/'] = requests.ConnectionError('https://server?key=' + KEY)
    with pytest.raises(OpenWebUIError) as caught:
        source.test_connection()
    assert KEY not in str(caught.value)


@pytest.mark.parametrize('url', ['https://user:password@host', 'ftp://host', 'https://host?token=secret', 'https://host/#fragment', 'http://host\\evil', 'http://host space'])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(OpenWebUIError):
        OpenWebUIApp(url, KEY)


def test_non_admin_token_is_not_accepted_for_backup(tmp_path):
    source, api = app()
    api.routes['/api/v1/auths/'] = ALICE
    with pytest.raises(OpenWebUIError, match='administrator'):
        source.backup(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_missing_original_file_discards_temporary_archive(tmp_path):
    source, api = app()
    original = api.request
    def missing(method, url, **kwargs):
        if url.endswith('/content'):
            return Reply({'detail': KEY}, status=404)
        return original(method, url, **kwargs)
    api.request = missing
    with pytest.raises(OpenWebUIError, match='404'):
        source.backup(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_truncated_original_file_discards_temporary_archive(tmp_path):
    source, api = app()
    api.routes['/api/v1/files/']['items'][0]['meta']['size'] = 5
    with pytest.raises(OpenWebUIError, match='size'):
        source.backup(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_repeated_paginated_data_is_not_silently_truncated():
    source, api = app()
    api.routes['/api/v1/users/']['total'] = 3
    with pytest.raises(OpenWebUIError, match='repeated'):
        source.pages('/api/v1/users/', 'users')


def test_user_restore_preview_has_no_writes_and_refuses_wrong_owner(exported):
    path, _ = exported
    target, api = app(True)
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com')
    assert restore.plan()['chats'] == 1
    assert api.writes == []
    api.routes['/api/v1/auths/']['email'] = 'someone-else@example.com'
    with pytest.raises(OpenWebUIError, match='different email'):
        restore.plan()
    assert api.writes == []


def test_user_restore_maps_files_knowledge_and_actor_without_importing_other_users(exported, caplog):
    path, _ = exported
    target, api = app(True)
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com')
    restore.plan()
    with caplog.at_level('INFO'):
        restore.apply()
    assert 'restored file 1/1' in caplog.text and 'restored file 2/1' not in caplog.text
    bodies = {path: kwargs.get('json') for path, kwargs in api.writes}
    chat = bodies['/api/v1/chats/import']['chats'][0]
    assert chat['variables'] == {'answer': '42'}
    assert chat['archived'] is True and chat['pinned'] is True
    assert 'folder_id' not in chat
    msg = chat['chat']['history']['messages']['m']
    assert msg['user_id'] == 'alice-new'
    assert msg['files'][0] == {'id': 'file-new', 'url': '/base/api/v1/files/file-new/content'}
    model = bodies['/api/v1/models/import']['models'][0]
    assert model['meta']['knowledge'][0]['id'] == 'knowledge-new'
    assert model['access_grants'] == []
    assert bodies['/api/v1/files/file-new/data/content/update'] == {'content': 'edited text'}
    assert bodies['/api/v1/tools/id/tool-a/valves/update'] == {'secret': 'tool-private'}
    assert '/api/v1/prompts/id/prompt-new/toggle' in bodies
    assert '/api/v1/configs/import' not in bodies
    assert len([p for p, _ in api.writes if p == '/api/v1/chats/import']) == 1


def test_skipped_model_is_detected_even_when_import_returns_true(exported):
    path, _ = exported
    target, api = app(True)
    original = api.request
    def skipped(method, url, **kwargs):
        if url.endswith('/models/import'):
            return Reply(True)
        return original(method, url, **kwargs)
    api.request = skipped
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com')
    restore.plan()
    with pytest.raises(OpenWebUIError, match='partially restored'):
        restore.apply()
    assert all(p != '/api/v1/chats/import' for p, _ in api.writes)


def test_nonempty_target_stops_before_any_write(exported):
    target, api = app(True)
    api.routes['/api/v1/chats/all'] = [{'id': 'existing'}]
    with pytest.raises(OpenWebUIError, match='already has chats'):
        OpenWebUIRestore(target, exported[0], source_user='alice@example.com').plan()
    assert api.writes == []


def test_version_mismatch_stops_before_any_write(exported):
    target, api = app(True)
    api.routes['/api/version']['version'] = '0.13.0'
    with pytest.raises(OpenWebUIError, match='same Open WebUI version'):
        OpenWebUIRestore(target, exported[0], source_user='alice@example.com').plan()
    assert api.writes == []


def test_archive_tampering_stops_before_server_requests(exported, tmp_path):
    original, _ = exported
    damaged = tmp_path / 'damaged.zip'
    with zipfile.ZipFile(original) as src, zipfile.ZipFile(damaged, 'w') as dst:
        for name in src.namelist():
            dst.writestr(name, b'changed' if name == 'files/file-a' else src.read(name))
    target, api = app(True)
    with pytest.raises(OpenWebUIError, match='checksum'):
        OpenWebUIRestore(target, damaged, source_user='alice@example.com').plan()
    assert api.calls == []


def test_config_restore_is_explicit_admin_only(exported):
    target, api = app(True)
    restore = OpenWebUIRestore(target, exported[0], component='config')
    with pytest.raises(OpenWebUIError, match='administrator'):
        restore.plan()
    api.routes['/api/v1/auths/'] = ADMIN
    assert restore.plan() == {'configuration': 1}
    assert not api.writes
    restore.apply()
    assert api.writes[0][0] == '/api/v1/configs/import'


def test_functions_restore_preserves_activation_and_valves(exported):
    target, api = app(True)
    api.routes['/api/v1/auths/'] = ADMIN
    restore = OpenWebUIRestore(target, exported[0], component='functions')
    restore.plan()
    restore.apply()
    value = api.writes[0][1]['json']['functions'][0]
    assert value['is_active'] and value['is_global']
    assert value['valves'] == {'secret': 'function-private'}


def test_app_is_registered_and_errors_remain_readable():
    import backup
    import config_store
    assert isinstance(backup.build_app('open_webui', {'url': 'http://host', 'api_key': KEY}), OpenWebUIApp)
    assert 'open_webui' in config_store.APP_NAMES
    assert config_store.app_meta('open_webui')['restore_supported'] is False
    message = backup.humanize_error(OpenWebUIError('open_webui: access denied'))
    assert 'access denied' in message


def test_config_encrypts_open_webui_key(isolated_webui):
    import config_store
    cfg = config_store.load_config()
    cfg['apps']['open_webui']['api_key'] = KEY
    config_store.save_config(cfg)
    assert KEY not in open(config_store.CONFIG_PATH).read()
    assert config_store.load_config()['apps']['open_webui']['api_key'] == KEY


def test_failed_replan_and_repeated_apply_cannot_write(exported):
    target, api = app(True)
    api.routes['/api/v1/auths/'] = ADMIN
    restore = OpenWebUIRestore(target, exported[0], component='config')
    restore.plan()
    api.routes['/api/version']['version'] = 'different'
    with pytest.raises(OpenWebUIError):
        restore.plan()
    with pytest.raises(OpenWebUIError, match='plan restore'):
        restore.apply()
    assert not api.writes
    api.routes['/api/version']['version'] = '0.12.3'
    restore.plan()
    restore.apply()
    with pytest.raises(OpenWebUIError, match='plan restore'):
        restore.apply()
    assert len(api.writes) == 1


def test_remapping_does_not_change_message_text():
    from apps.open_webui_restore import remap
    value = {'content': 'file-a', 'role': 'alice-old', 'name': 'file-a',
             'id': 'file-a', 'user_id': 'alice-old',
             'url': 'https://old.example/api/v1/files/file-a/content'}
    changed = remap(value, {'file-a': 'file-new', 'alice-old': 'alice-new'})
    assert changed['content'] == 'file-a' and changed['role'] == 'alice-old' and changed['name'] == 'file-a'
    assert changed['id'] == 'file-new' and changed['user_id'] == 'alice-new'
    assert changed['url'] == '/api/v1/files/file-new/content'


def test_chat_file_not_in_admin_listing_is_downloaded(tmp_path):
    source, api = app()
    original_file = api.routes['/api/v1/files/']['items'][0]
    api.routes['/api/v1/files/'] = page([])
    api.routes['/api/v1/files/file-a'] = original_file
    api.routes['/api/v1/knowledge/'] = page([])
    api.routes['/api/v1/models/export'] = []
    path = source.backup(tmp_path)
    with zipfile.ZipFile(path) as z:
        assert z.read('files/file-a') == b'data'
        assert len(json.loads(z.read('files.json'))) == 1


def test_missing_referenced_upload_fails_export_before_archive_creation(tmp_path):
    source, api = app()
    api.routes['/api/v1/files/'] = page([])
    api.routes['/api/v1/knowledge/'] = page([])
    api.routes['/api/v1/models/export'] = []
    api.routes['/api/v1/files/file-a'] = Reply({}, status=404)
    with pytest.raises(OpenWebUIError, match='404'):
        source.backup(tmp_path)
    assert not list(tmp_path.iterdir())


def test_hidden_existing_model_is_not_overwritten(exported):
    target, api = app(True)
    api.routes['/api/v1/auths/']['role'] = 'admin'
    api.model = {'id': 'my-model', 'user_id': 'someone-else'}
    with pytest.raises(OpenWebUIError, match='conflicting model identifier'):
        OpenWebUIRestore(target, exported[0], source_user='alice@example.com').plan()
    assert not api.writes


def test_mfa_policies_are_not_in_importable_config(tmp_path):
    source, api = app()
    api.routes['/api/v1/configs/export']['auth.mfa.enable'] = True
    path = source.backup(tmp_path)
    with zipfile.ZipFile(path) as z:
        assert 'auth.mfa.enable' not in json.loads(z.read('config.json'))
        assert 'auth.mfa.enable' in json.loads(z.read('manifest.json'))['excluded_config_keys']


def test_false_success_on_indexed_content_update_is_rejected(exported):
    target, api = app(True)
    original = api.request
    def fail(method, url, **kwargs):
        if url.endswith('/data/content/update'):
            return Reply({'content': 'wrong'})
        return original(method, url, **kwargs)
    api.request = fail
    restore = OpenWebUIRestore(target, exported[0], source_user='alice@example.com')
    restore.plan()
    with pytest.raises(OpenWebUIError, match='indexed file text was not restored'):
        restore.apply()
    assert all(p != '/api/v1/chats/import' for p, _ in api.writes)


def test_encrypted_per_user_valves_are_excluded_from_export(tmp_path):
    source, api = app()
    # Deepcopy avoids mutating the shared fixture constants.
    api.routes['/api/v1/users/'] = copy.deepcopy(api.routes['/api/v1/users/'])
    settings = api.routes['/api/v1/users/']['users'][1]['settings']
    settings.update({'tools': {'valves': {'secret': 'ciphertext'}, 'other': True},
                     'functions': {'valves': {'secret': 'ciphertext'}}})
    path = source.backup(tmp_path)
    with zipfile.ZipFile(path) as z:
        restored = json.loads(z.read('users.json'))[1]['settings']
        assert 'valves' not in restored['tools'] and 'valves' not in restored['functions']
        assert restored['tools']['other'] is True


def test_rejected_settings_do_not_report_restore_success(exported):
    target, api = app(True)
    original = api.request
    def missing(method, url, **kwargs):
        if url.endswith('/settings/update'):
            return Reply({'ui': {}})
        return original(method, url, **kwargs)
    api.request = missing
    restore = OpenWebUIRestore(target, exported[0], source_user='alice@example.com')
    restore.plan()
    with pytest.raises(OpenWebUIError, match='discarded some user settings'):
        restore.apply()


def test_shared_knowledge_is_recovered_as_private_copy(tmp_path):
    source, api = app()
    api.routes['/api/v1/knowledge/']['items'][0]['user_id'] = 'admin-old'
    path = source.backup(tmp_path)
    target, api = app(True)
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com', include_shared_resources=True)
    assert restore.plan()['knowledge'] == 1
    restore.apply()
    create = next(kwargs['json'] for p, kwargs in api.writes if p == '/api/v1/knowledge/create')
    assert create['access_grants'] == []


def test_message_text_cannot_select_another_users_private_knowledge(tmp_path):
    source, api = app()
    api.routes['/api/v1/chats/all/db'][0]['chat']['history']['messages']['m']['content'] = 'private-admin-knowledge'
    api.routes['/api/v1/knowledge/'] = page([
        api.routes['/api/v1/knowledge/']['items'][0],
        {'id': 'private-admin-knowledge', 'user_id': 'admin-old', 'name': 'Private', 'description': '', 'meta': {}},
    ])
    api.routes['/api/v1/knowledge/private-admin-knowledge/files'] = page([])
    path = source.backup(tmp_path)
    target, _ = app(True)
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com')
    assert restore.plan()['knowledge'] == 1
    assert [k['id'] for k in restore.selected['knowledge']] == ['knowledge-a']


def test_structured_file_collection_name_is_remapped_without_rewriting_text():
    from apps.open_webui_restore import remap
    from apps.open_webui import file_references
    value = {'collection_name': 'file-file-a', 'content': 'file-file-a'}
    assert file_references(value) == {'file-a'}
    assert remap(value, {'file-a': 'file-new'}) == {'collection_name': 'file-file-new', 'content': 'file-file-a'}


@pytest.mark.parametrize('field', ['backgroundImageUrl', 'profile_image_url'])
def test_user_background_and_model_avatar_are_file_references(field):
    from apps.open_webui_restore import remap
    from apps.open_webui import file_references
    value = {field: '/api/v1/files/file-a/content'}
    assert file_references(value) == {'file-a'}
    assert remap(value, {'file-a': 'file-new'}) == {field: '/api/v1/files/file-new/content'}


def test_referenced_private_knowledge_is_fetched_despite_admin_listing_scope(tmp_path):
    source, api = app()
    knowledge = api.routes['/api/v1/knowledge/']['items'][0]
    api.routes['/api/v1/knowledge/'] = page([])
    api.routes['/api/v1/knowledge/knowledge-a'] = knowledge
    path = source.backup(tmp_path)
    with zipfile.ZipFile(path) as z:
        assert json.loads(z.read('knowledge.json'))[0]['id'] == 'knowledge-a'


def test_client_controlled_foreign_knowledge_reference_requires_explicit_consent(tmp_path):
    source, api = app()
    api.routes['/api/v1/knowledge/']['items'][0]['user_id'] = 'admin-old'
    path = source.backup(tmp_path)
    target, api = app(True)
    with pytest.raises(OpenWebUIError, match='not proof of access'):
        OpenWebUIRestore(target, path, source_user='alice@example.com').plan()
    assert not api.writes
    preview = OpenWebUIRestore(target, path, source_user='alice@example.com', include_shared_resources=True).plan()
    assert preview['shared_resources_requiring_operator_verified_access']['knowledge'] == [
        {'id': 'knowledge-a', 'owner': 'admin@example.com'}]


def test_client_controlled_foreign_file_reference_requires_explicit_consent(tmp_path):
    source, api = app()
    api.routes['/api/v1/files/']['items'][0]['user_id'] = 'admin-old'
    path = source.backup(tmp_path)
    target, api = app(True)
    with pytest.raises(OpenWebUIError, match='not proof of access'):
        OpenWebUIRestore(target, path, source_user='alice@example.com').plan()
    assert not api.writes


def test_bare_file_url_is_detected_and_remapped():
    from apps.open_webui_restore import remap
    from apps.open_webui import file_references
    value = {'files': [{'type': 'file', 'url': 'file-a', 'name': 'Note'}]}
    assert file_references(value) == {'file-a'}
    assert remap(value, {'file-a': 'file-new'})['files'][0]['url'] == 'file-new'


def test_settings_foreign_background_requires_the_same_access_opt_in(tmp_path):
    source, api = app()
    api.routes['/api/v1/users/'] = copy.deepcopy(api.routes['/api/v1/users/'])
    api.routes['/api/v1/users/']['users'][1]['settings']['ui']['backgroundImageUrl'] = '/api/v1/files/background-a/content'
    file = {'id': 'background-a', 'user_id': 'admin-old', 'filename': 'background.png', 'meta': {'size': 4}, 'data': {}}
    api.routes['/api/v1/files/'] = page(api.routes['/api/v1/files/']['items'] + [file])
    api.routes['/api/v1/files/background-a/content'] = Reply(raw=b'data')
    path = source.backup(tmp_path)
    target, api = app(True)
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com')
    with pytest.raises(OpenWebUIError, match='not proof of access'):
        restore.plan()
    assert not restore.planned and not api.writes
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com', include_shared_resources=True)
    assert restore.plan()['shared_resource_counts']['files'] == 1


def test_multipart_upload_streams_without_unbounded_reads_and_quotes_filename():
    import io
    from apps.open_webui_restore import multipart_upload
    class BoundedOnly(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 16384
            return super().read(size)
    body = b'x' * 40000
    encoder = multipart_upload(BoundedOnly(body), len(body), 'report"\r\nheader.pdf', 'application/pdf\r\nInjected: yes')
    prepared = requests.Request('POST', 'http://target/upload', data=encoder,
                                headers={'Content-Type': encoder.content_type}).prepare()
    assert int(prepared.headers['Content-Length']) == encoder.len
    chunks = []
    while chunk := prepared.body.read(16384):
        chunks.append(chunk)
    payload = b''.join(chunks)
    assert len(payload) == encoder.len
    assert body in payload
    assert b'Content-Type: application/octet-stream' in payload
    assert b'Injected:' not in payload and b'\r\nheader.pdf' not in payload
    assert b'filename="report%22header.pdf"' in payload


@pytest.mark.parametrize('kind', ['chat', 'note', 'folder', 'filesystem', 'text', 'web_search'])
def test_other_attachment_ids_are_not_treated_as_uploads_or_knowledge(kind):
    from apps.open_webui import file_references, knowledge_references
    value = {'files': [{'type': kind, 'id': 'not-a-file', 'collection_name': 'temporary-collection'}]}
    assert file_references(value) == set()
    assert knowledge_references(value) == set()


def test_unsupported_linked_chat_does_not_break_export_but_restore_fails_before_writes(tmp_path):
    source, api = app()
    api.routes['/api/v1/chats/all/db'][0]['chat']['files'] = [{'type': 'chat', 'id': 'another-chat'}]
    path = source.backup(tmp_path)
    assert all('/files/another-chat' not in url for _, url, _ in api.calls)
    target, api = app(True)
    restore = OpenWebUIRestore(target, path, source_user='alice@example.com')
    with pytest.raises(OpenWebUIError, match='unsupported linked resources'):
        restore.plan()
    assert not restore.planned and not api.writes


def test_uploaded_image_with_bare_url_is_included_but_generated_cache_is_not():
    from apps.open_webui import file_references
    assert file_references({'files': [{'type': 'image', 'id': 'image-a', 'url': 'image-a', 'file': {'id': 'image-a'}}]}) == {'image-a'}
    assert file_references({'files': [{'type': 'image', 'id': 'cache-a', 'url': '/cache/image.png'}]}) == set()
