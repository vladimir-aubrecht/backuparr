"""Open WebUI API export. No database files or server volumes are accessed.

API contracts and recovery limitations: docs/open-webui.md. Archives contain
private conversations, uploaded files and configuration secrets.
"""
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

log = logging.getLogger('backuparr.open_webui')
FORMAT = 'backuparr-open-webui-api-v1'
API_SOURCE = '91ea4e3421d1d8069f13d4a463fcb5986d94ea60'
MFA_CONFIG_KEYS = {'auth.mfa.enable', 'auth.mfa.allow_oauth_bypass', 'auth.mfa.allow_trusted_header_bypass'}
URL_FIELDS = {'url', 'file_url', 'image_url', 'background_image_url', 'backgroundImageUrl', 'profile_image_url', 'download_url'}


def file_references(value, key=None):
    refs = set()
    if isinstance(value, dict):
        if value.get('type') == 'file' and isinstance(value.get('id'), str):
            refs.add(value['id'])
        image_id = value.get('id')
        uploaded_image = (key == 'files' and value.get('type') == 'image' and isinstance(image_id, str) and
                          (value.get('url') == image_id or
                           (isinstance(value.get('file'), dict) and value['file'].get('id') == image_id)))
        if uploaded_image:
            refs.add(image_id)
        if key == 'files' and value.get('type') in (None, 'file'):
            if isinstance(value.get('id'), str):
                refs.add(value['id'])
            bare_url = value.get('url')
            if isinstance(bare_url, str) and re.fullmatch(r'[A-Za-z0-9_-]+', bare_url):
                refs.add(bare_url)
        for name, child in value.items():
            refs.update(file_references(child, name))
    elif isinstance(value, list):
        for child in value:
            refs.update(file_references(child, key))
    elif isinstance(value, str):
        if key in {'file_id', 'fileId', 'file_ids'}:
            refs.add(value)
        if key in {'collection_name', 'collection_names'} and value.startswith('file-'):
            refs.add(value[5:])
        if key in URL_FIELDS:
            refs.update(re.findall(r'/api/v1/files/([A-Za-z0-9_-]+)', value))
    return refs


def knowledge_references(value, key=None):
    refs = set()
    if isinstance(value, dict):
        if (key == 'knowledge' or (key == 'files' and value.get('type') == 'collection')) and value.get('type') != 'file':
            if isinstance(value.get('id'), str):
                refs.add(value['id'])
        for name, child in value.items():
            refs.update(knowledge_references(child, name))
    elif isinstance(value, list):
        for child in value:
            refs.update(knowledge_references(child, key))
    elif isinstance(value, str) and value and key in {'knowledge_id', 'knowledge_ids'}:
        if not value.startswith('file-'):
            refs.add(value)
    return refs

LIMITATIONS = [
    'API export, not a complete server/database snapshot; writes during export are not transactionally isolated.',
    'Accounts, passwords, API keys, MFA, OAuth/SSO, group memberships and sharing permissions require manual setup.',
    'Workspace and files are limited by the administrator\'s access; enable BYPASS_ADMIN_ACCESS_CONTROL for other users\' private workspace data.',
    'No model weights, external provider state, environment variables, vector database, generated cache assets or external knowledge connector data.',
    'Knowledge documents are reindexed on restore; directory layout is not restored.',
    'Chats keep message content, history, tags, timestamps, pin/archive state; folder hierarchy and public share links are not restored.',
    'Custom tools/functions run Python on import. Restore only an archive you trust.',
]


class OpenWebUIError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise OpenWebUIError('open_webui: unexpected resource identifier')
    return value


def objects(value, label):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise OpenWebUIError(f'open_webui: invalid {label} response')
    return value


class OpenWebUIApp:
    def __init__(self, url, api_key, timeout=60):
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or
                parts.username is not None or parts.password is not None or parts.query or parts.fragment or
                any(c.isspace() for c in url) or '\\' in url):
            raise OpenWebUIError('open_webui: use an HTTP(S) server URL without credentials, query or fragment')
        if not isinstance(api_key, str) or not api_key.strip() or '\n' in api_key or '\r' in api_key:
            raise OpenWebUIError('open_webui: an API key is required')
        self.url, self.timeout = url.rstrip('/'), timeout
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {api_key}'})

    def _request(self, method, path, **kwargs):
        # All callers construct paths locally. Never follow a redirect with a key.
        try:
            reply = self.session.request(method, self.url + path, timeout=self.timeout,
                                         allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise OpenWebUIError('open_webui: request failed; check server connectivity') from None
        if reply.status_code not in (200, 201, 204):
            status = reply.status_code
            reply.close()
            if 300 <= status < 400:
                raise OpenWebUIError('open_webui: redirect refused; use the final server URL')
            if status in (401, 403):
                raise OpenWebUIError('open_webui: access denied; check administrator API key, API endpoint restrictions and ENABLE_ADMIN_EXPORT')
            raise OpenWebUIError(f'open_webui: API request failed (HTTP {status})', status=status)
        return reply

    def _json(self, method, path, **kwargs):
        reply = self._request(method, path, **kwargs)
        try:
            if reply.status_code == 204:
                return None
            return reply.json()
        except (ValueError, requests.RequestException):
            raise OpenWebUIError('open_webui: API returned invalid JSON') from None
        finally:
            reply.close()

    def account(self, admin=False):
        account = self._json('GET', '/api/v1/auths/')
        if not isinstance(account, dict) or not account.get('id') or not account.get('email'):
            raise OpenWebUIError('open_webui: invalid account response')
        identifier(account['id'])
        if admin and account.get('role') != 'admin':
            raise OpenWebUIError('open_webui: backup requires an administrator API key')
        return {key: account.get(key) for key in ('id', 'email', 'name', 'role')}

    def test_connection(self):
        self.account(admin=True)
        # Authenticated/admin-only; public /api/version does not validate access.
        config = self._json('GET', '/api/v1/configs/export')
        if not isinstance(config, dict):
            raise OpenWebUIError('open_webui: invalid configuration export')
        return 'Open WebUI administrator API access OK (limited API export; see backup scope)'

    def pages(self, path, key='items'):
        out, seen, expected = [], set(), None
        for page in range(1, 100001):
            data = self._json('GET', path, params={'page': page})
            if not isinstance(data, dict) or not isinstance(data.get('total'), int) or data['total'] < 0:
                raise OpenWebUIError('open_webui: unsupported pagination response')
            rows = objects(data.get(key), 'paginated')
            if expected is not None and data['total'] != expected:
                raise OpenWebUIError('open_webui: data changed during export; retry when idle')
            expected = data['total']
            for row in rows:
                item_id = identifier(row.get('id'))
                if item_id in seen:
                    raise OpenWebUIError('open_webui: repeated page or changing data during export')
                seen.add(item_id)
                out.append(row)
            if len(out) == expected:
                return out
            if not rows or len(out) > expected:
                raise OpenWebUIError('open_webui: incomplete paginated export')
        raise OpenWebUIError('open_webui: pagination limit exceeded')

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        account = self.account(admin=True)
        version = self._json('GET', '/api/version')
        if not isinstance(version, dict) or not isinstance(version.get('version'), str):
            raise OpenWebUIError('open_webui: invalid server version response')
        # Fail closed on permission errors instead of quietly exporting an empty set.
        users = self.pages('/api/v1/users/', 'users')
        user_ids = {u['id'] for u in users}
        chats = objects(self._json('GET', '/api/v1/chats/all/db'), 'chats')
        for chat in chats:
            if chat.get('user_id') not in user_ids or not isinstance(chat.get('chat'), dict):
                raise OpenWebUIError('open_webui: chat owner or content is missing; export aborted')
        config = self._json('GET', '/api/v1/configs/export')
        if not isinstance(config, dict):
            raise OpenWebUIError('open_webui: invalid configuration export')
        excluded_config = sorted(MFA_CONFIG_KEYS & config.keys())
        config = {k: v for k, v in config.items() if k not in MFA_CONFIG_KEYS}
        models = objects(self._json('GET', '/api/v1/models/export'), 'models')
        # /prompts/ excludes disabled prompts on current Open WebUI; /list does not.
        prompts = self.pages('/api/v1/prompts/list')
        tools = objects(self._json('GET', '/api/v1/tools/export'), 'tools')
        for tool in tools:
            item_id = identifier(tool.get('id'))
            tool['valves'] = self._json('GET', f'/api/v1/tools/id/{item_id}/valves')
        functions = objects(self._json('GET', '/api/v1/functions/export', params={'include_valves': 'true'}), 'functions')
        # Older servers ignore include_valves: request the explicit endpoint too.
        for function in functions:
            item_id = identifier(function.get('id'))
            function['valves'] = self._json('GET', f'/api/v1/functions/id/{item_id}/valves')
        files = self.pages('/api/v1/files/')
        known_files = {f['id'] for f in files}
        knowledge = self.pages('/api/v1/knowledge/')
        known_knowledge = {k['id'] for k in knowledge}
        for item_id in sorted(knowledge_references([chats, models, users]) - known_knowledge):
            item = self._json('GET', f'/api/v1/knowledge/{identifier(item_id)}')
            if not isinstance(item, dict) or item.get('id') != item_id:
                raise OpenWebUIError('open_webui: referenced knowledge metadata is missing; export aborted')
            knowledge.append(item)
            known_knowledge.add(item_id)
        excluded_knowledge = []
        saved_knowledge = []
        for item in knowledge:
            meta = item.get('meta') or {}
            if meta.get('external') or meta.get('source') == 'external':
                excluded_knowledge.append(item['id'])
                continue
            members = self.pages(f'/api/v1/knowledge/{identifier(item["id"])}/files')
            item['file_ids'] = [f['id'] for f in members]
            for member in members:
                if member['id'] not in known_files:
                    # A shared knowledge base can expose files absent from /files/.
                    file = self._json('GET', f'/api/v1/files/{identifier(member["id"])}')
                    if not isinstance(file, dict) or file.get('id') != member['id']:
                        raise OpenWebUIError('open_webui: incomplete knowledge file metadata')
                    files.append(file)
                    known_files.add(file['id'])
            saved_knowledge.append(item)
        # Admin GET /files/{id} can read an upload even when admin bypass is
        # disabled and the global file listing omits another owner's file.
        for item_id in sorted(file_references([chats, models, users]) - known_files):
            file = self._json('GET', f'/api/v1/files/{identifier(item_id)}')
            if not isinstance(file, dict) or file.get('id') != item_id:
                raise OpenWebUIError('open_webui: referenced upload metadata is missing; export aborted')
            files.append(file)
            known_files.add(item_id)
        owners = [{key: copy.deepcopy(u.get(key)) for key in ('id', 'email', 'name', 'role', 'settings')} for u in users]
        for owner in owners:
            # These values are ciphertext bound to the source server's secret.
            # They are not plaintext API exports and cannot be re-imported safely.
            settings = owner.get('settings')
            if isinstance(settings, dict):
                for category in ('tools', 'functions'):
                    if isinstance(settings.get(category), dict):
                        settings[category].pop('valves', None)
        data = {'users': owners, 'chats': chats, 'config': config, 'models': models, 'prompts': prompts,
                'tools': tools, 'functions': functions, 'files': files, 'knowledge': saved_knowledge}
        for name in ('models', 'prompts', 'tools', 'knowledge'):
            if any(row.get('user_id') not in user_ids for row in data[name]):
                raise OpenWebUIError(f'open_webui: {name} owner is missing; export aborted')
        manifest = {'format': FORMAT, 'version': version['version'], 'created_at': datetime.now(timezone.utc).isoformat(),
                    'account': account, 'api_source_commit': API_SOURCE, 'limitations': LIMITATIONS,
                    'excluded_external_knowledge_ids': excluded_knowledge, 'excluded_config_keys': excluded_config, 'sha256': {}}
        fd, temporary = tempfile.mkstemp(prefix='.open-webui-', suffix='.zip', dir=dest_dir)
        final = os.path.join(dest_dir, 'open-webui-api-export.zip')
        try:
            with os.fdopen(fd, 'w+b') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
                def write(name, value):
                    blob = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
                    archive.writestr(name, blob)
                    manifest['sha256'][name] = hashlib.sha256(blob).hexdigest()
                for name, values in data.items():
                    write(name + '.json', values)
                    log.info('open_webui: exported %s', name)
                for index, file in enumerate(files, 1):
                    item_id = identifier(file['id'])
                    name = f'files/{item_id}'
                    digest = hashlib.sha256()
                    response = self._request('GET', f'/api/v1/files/{item_id}/content', stream=True)
                    size = 0
                    try:
                        with archive.open(name, 'w') as target:
                            for chunk in response.iter_content(1024 * 1024):
                                target.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                    except requests.RequestException:
                        raise OpenWebUIError('open_webui: file download interrupted; export discarded') from None
                    finally:
                        response.close()
                    declared = (file.get('meta') or {}).get('size')
                    if isinstance(declared, int) and size != declared:
                        raise OpenWebUIError('open_webui: uploaded file size does not match; export discarded')
                    manifest['sha256'][name] = digest.hexdigest()
                    log.info('open_webui: downloaded file %s/%s', index, len(files))
                archive.writestr('manifest.json', json.dumps(manifest, indent=2))
                archive.writestr('RECOVERY.txt', '\n'.join(LIMITATIONS) + '\nSee docs/open-webui.md and python -m apps.open_webui_restore --help.\n')
            os.replace(temporary, final)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        log.warning('open_webui: API export complete; accounts and database require separate recovery (see RECOVERY.txt)')
        return final
