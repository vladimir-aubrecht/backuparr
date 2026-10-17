"""Explicit, preview-first restore of an Open WebUI API export.

Run with --help; recovery order and limitations are in docs/open-webui.md.
"""
import argparse
import copy
import hashlib
import json
import logging
import re
import sys
import zipfile

from requests_toolbelt.multipart.encoder import MultipartEncoder
from pathlib import Path
from urllib.parse import urlsplit

from apps.open_webui import FORMAT, MFA_CONFIG_KEYS, URL_FIELDS, OpenWebUIApp, OpenWebUIError, file_references, identifier, knowledge_references, objects

JSON_NAMES = {'users', 'chats', 'config', 'models', 'prompts', 'tools', 'functions', 'files', 'knowledge'}
CHAT_FIELDS = {'chat', 'meta', 'pinned', 'archived', 'current_message_id', 'created_at', 'updated_at', 'variables'}


class ArchiveUpload:
    """Give MultipartEncoder a remaining length without buffering ZipExtFile."""
    def __init__(self, stream, length):
        self.stream, self.len = stream, length

    def read(self, size=-1):
        size = min(size if size is not None and size >= 0 else 64 * 1024, self.len)
        data = self.stream.read(size)
        if not data and self.len:
            raise OpenWebUIError('open_webui: archive upload ended early')
        self.len -= len(data)
        return data


def multipart_upload(stream, size, filename, content_type):
    if not isinstance(filename, str):
        filename = 'upload'
    filename = filename.replace('\r', '').replace('\n', '')
    if not isinstance(content_type, str) or not re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', content_type):
        content_type = 'application/octet-stream'
    return MultipartEncoder(fields={'file': (filename, ArchiveUpload(stream, size), content_type)})


def unsupported_chat_links(value, key=None):
    types = set()
    if isinstance(value, dict):
        if key == 'files' and value.get('type') in {'note', 'chat', 'folder', 'filesystem'}:
            types.add(value['type'])
        if key == 'files' and value.get('type') in {'text', 'web_search'} and value.get('collection_name'):
            types.add(value['type'])
        for name, child in value.items():
            types.update(unsupported_chat_links(child, name))
    elif isinstance(value, list):
        for child in value:
            types.update(unsupported_chat_links(child, key))
    return types


def remap(value, mapping, *, key=None, target_prefix=''):
    """Map structured resource references; ordinary conversation text is immutable."""
    if isinstance(value, list):
        return [remap(x, mapping, key=key, target_prefix=target_prefix) for x in value]
    if isinstance(value, dict):
        return {name: remap(v, mapping, key=name, target_prefix=target_prefix) for name, v in value.items()}
    if isinstance(value, str):
        if key in {'id', 'file_id', 'fileId', 'user_id', 'userId', 'knowledge_id', 'collection_id',
                   'file_ids', 'knowledge_ids', 'ids', 'collection_name', 'collection_names'} and value in mapping:
            return mapping[value]
        if key in {'collection_name', 'collection_names'} and value.startswith('file-') and value[5:] in mapping:
            return 'file-' + mapping[value[5:]]
        if key in URL_FIELDS:
            if value in mapping:
                return mapping[value]
            match = re.search(r'(/api/v1/files/)([A-Za-z0-9_-]+)(/[^?#]*)?', value)
            if match and match[2] in mapping:
                # Browser-relative URL; never point restored content at the old host.
                return target_prefix + match[1] + mapping[match[2]] + (match[3] or '')
    return value


def contains_settings(actual, expected):
    """An update may merge defaults, but must not silently discard saved values."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(contains_settings(actual.get(k), v) for k, v in expected.items())
    return actual == expected


class OpenWebUIRestore:
    def __init__(self, app, archive, *, component='user', source_user=None, include_shared_resources=False):
        self.app, self.archive = app, str(archive)
        self.component, self.source_user = component, source_user
        self.include_shared_resources = include_shared_resources
        self.data = None
        self.planned = False

    def _load(self):
        try:
            with zipfile.ZipFile(self.archive) as z:
                names = z.namelist()
                if len(names) != len(set(names)):
                    raise OpenWebUIError('open_webui: duplicate archive members')
                manifest = json.loads(z.read('manifest.json'))
                if not isinstance(manifest, dict):
                    raise OpenWebUIError('open_webui: invalid archive manifest')
                if manifest.get('format') != FORMAT:
                    raise OpenWebUIError('open_webui: unsupported archive format')
                hashes = manifest.get('sha256')
                if not isinstance(hashes, dict) or not all(n + '.json' in hashes for n in JSON_NAMES):
                    raise OpenWebUIError('open_webui: incomplete archive manifest')
                if set(names) != set(hashes) | {'manifest.json', 'RECOVERY.txt'}:
                    raise OpenWebUIError('open_webui: unexpected or missing archive members')
                for name, expected in hashes.items():
                    if name not in {n + '.json' for n in JSON_NAMES} and not re.fullmatch(r'files/[A-Za-z0-9_-]+', name):
                        raise OpenWebUIError('open_webui: unexpected archive member')
                    digest = hashlib.sha256()
                    with z.open(name) as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                            digest.update(chunk)
                    if digest.hexdigest() != expected:
                        raise OpenWebUIError('open_webui: archive checksum mismatch')
                data = {n: json.loads(z.read(n + '.json')) for n in JSON_NAMES}
                if not isinstance(data['config'], dict):
                    raise OpenWebUIError('open_webui: invalid archived configuration')
                for name in JSON_NAMES - {'config'}:
                    objects(data[name], name)
                file_ids = [identifier(f.get('id')) for f in data['files']]
                if len(set(file_ids)) != len(file_ids) or {f'files/{i}' for i in file_ids} != {n for n in hashes if n.startswith('files/')}:
                    raise OpenWebUIError('open_webui: incomplete archived files')
                return manifest, data
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
            raise OpenWebUIError('open_webui: invalid or unreadable archive') from None

    def plan(self):
        self.planned = False
        self.manifest, self.data = self._load()
        self.account = self.app.account(admin=self.component != 'user')
        version = self.app._json('GET', '/api/version')
        if not isinstance(version, dict) or version.get('version') != self.manifest.get('version'):
            raise OpenWebUIError('open_webui: restore to the same Open WebUI version as the export, then upgrade')
        if self.component == 'config':
            if MFA_CONFIG_KEYS & self.data['config'].keys():
                raise OpenWebUIError('open_webui: MFA policy must be restored manually with an interactive admin session')
            self.summary = {'configuration': 1}
        elif self.component == 'functions':
            if self.app._json('GET', '/api/v1/functions/export'):
                raise OpenWebUIError('open_webui: restore functions into an empty function workspace')
            self.summary = {'functions': len(self.data['functions'])}
        elif self.component == 'user':
            users = [u for u in self.data['users'] if isinstance(u.get('email'), str) and
                     u['email'].casefold() == (self.source_user or '').casefold()]
            if len(users) != 1:
                raise OpenWebUIError('open_webui: select one archived account with --source-user EMAIL')
            self.source = users[0]
            if self.account['email'].casefold() != self.source['email'].casefold():
                raise OpenWebUIError('open_webui: target token belongs to a different email address')
            uid = self.source['id']
            self.selected = {name: [x for x in self.data[name] if x.get('user_id') == uid]
                             for name in ('chats', 'models', 'prompts', 'tools', 'knowledge', 'files')}
            unsupported = unsupported_chat_links(self.selected['chats'])
            if unsupported:
                raise OpenWebUIError('open_webui: chats contain unsupported linked resources (' + ', '.join(sorted(unsupported)) + '); recover the conversation content manually and recreate these attachments')
            # References to shared uploads must also be recovered for this owner.
            file_ids = {f['id'] for f in self.data['files']}
            needed = {f['id'] for f in self.selected['files']}
            def referenced(value):
                refs = file_references(value)
                if refs - file_ids:
                    raise OpenWebUIError('open_webui: content references uploads missing from the archive')
                needed.update(refs)
            referenced([self.selected, self.source.get('settings')])
            # A model/chat may refer to a shared local knowledge base owned by
            # someone else. Recover a private copy with its documents for this user.
            known_knowledge = {k['id']: k for k in self.data['knowledge']}
            selected_knowledge = {k['id'] for k in self.selected['knowledge']}
            refs = knowledge_references([self.selected, self.source.get('settings')])
            if refs - known_knowledge.keys():
                raise OpenWebUIError('open_webui: content references knowledge missing from the archive')
            selected_knowledge.update(refs)
            self.selected['knowledge'] = [k for k in self.data['knowledge'] if k['id'] in selected_knowledge]
            referenced(self.selected['knowledge'])
            # External/unavailable knowledge cannot be made functional by restoring
            # only its identifier. Reject before uploading any target data.
            for model in self.selected['models']:
                for ref in (model.get('meta') or {}).get('knowledge') or []:
                    if isinstance(ref, dict) and ref.get('id') not in needed | selected_knowledge:
                        raise OpenWebUIError('open_webui: model references knowledge not included in the archive')
            self.selected['files'] = [f for f in self.data['files'] if f['id'] in needed]
            self.foreign = {kind: [item for item in self.selected[kind] if item.get('user_id') != uid]
                            for kind in ('files', 'knowledge')}
            if any(self.foreign.values()) and not self.include_shared_resources:
                raise OpenWebUIError('open_webui: referenced files/knowledge belong to other users. Verify their original sharing permissions before using --include-shared-resources; chat references are not proof of access')
            for knowledge in self.selected['knowledge']:
                if not isinstance(knowledge.get('file_ids'), list) or any(i not in needed for i in knowledge['file_ids']):
                    raise OpenWebUIError('open_webui: knowledge references unavailable files')
            for tool in self.selected['tools']:
                if not isinstance(tool.get('content'), str):
                    raise OpenWebUIError('open_webui: archived tool source is missing')
            self._check_empty_target()
            self.summary = {k: len(v) for k, v in self.selected.items()}
            self.summary['settings'] = int(isinstance(self.source.get('settings'), dict))
            if any(self.foreign.values()):
                self.summary['shared_resource_counts'] = {kind: len(items) for kind, items in self.foreign.items()}
                emails = {u['id']: u.get('email') for u in self.data['users']}
                self.summary['shared_resources_requiring_operator_verified_access'] = {
                    kind: [{'id': item['id'], 'owner': emails.get(item.get('user_id'), 'unknown')} for item in items]
                    for kind, items in self.foreign.items() if items}
        else:
            raise OpenWebUIError('open_webui: unknown restore component')
        self.planned = True
        return self.summary

    def _check_empty_target(self):
        # GET /chats/all is NDJSON on current versions, JSON on older releases.
        reply = self.app._request('GET', '/api/v1/chats/all')
        try:
            content = reply.text.strip()
            if content not in ('', '[]'):
                raise OpenWebUIError('open_webui: target account already has chats; use a fresh account')
        finally:
            reply.close()
        uid = self.account['id']
        checks = [self.app.pages('/api/v1/files/'), self.app.pages('/api/v1/knowledge/'),
                  self.app.pages('/api/v1/prompts/list'),
                  objects(self.app._json('GET', '/api/v1/models/export'), 'models'),
                  objects(self.app._json('GET', '/api/v1/tools/export'), 'tools')]
        if any(x.get('user_id') == uid for rows in checks for x in rows):
            raise OpenWebUIError('open_webui: target account workspace is not empty; use a fresh account')
        # The import API lets admins overwrite even hidden existing model IDs.
        # Probe each specific ID before uploading anything, regardless of listing.
        for model in self.selected['models']:
            try:
                existing = self.app._json('GET', '/api/v1/models/model', params={'id': model['id']})
            except OpenWebUIError as exc:
                if exc.status != 404:
                    raise
            else:
                if existing is not None:
                    raise OpenWebUIError('open_webui: target contains a conflicting model identifier')
        # Model/tool IDs and prompt commands are global: never overwrite another owner.
        target_models, target_tools, target_prompts = checks[3], checks[4], checks[2]
        for old, current, field in [(self.selected['models'], target_models, 'id'),
                                    (self.selected['tools'], target_tools, 'id'),
                                    (self.selected['prompts'], target_prompts, 'command')]:
            if {x.get(field) for x in old} & {x.get(field) for x in current}:
                raise OpenWebUIError('open_webui: target contains conflicting workspace resources')

    def apply(self):
        if not self.planned:
            raise OpenWebUIError('open_webui: plan restore before applying')
        self.planned = False  # A plan is single-use, including a failed apply.
        # Detect an archive replaced between preview and apply, before any write.
        current, _ = self._load()
        if current != self.manifest:
            raise OpenWebUIError('open_webui: archive changed after preview')
        if self.component == 'config':
            self.app._json('POST', '/api/v1/configs/import', json={'config': self.data['config']})
            return self.summary
        if self.component == 'functions':
            if self.app._json('GET', '/api/v1/functions/export'):
                raise OpenWebUIError('open_webui: target functions changed after preview')
            result = objects(self.app._json('POST', '/api/v1/functions/sync', json={'functions': self.data['functions']}), 'restored functions')
            if {f.get('id') for f in result} != {f.get('id') for f in self.data['functions']}:
                raise OpenWebUIError('open_webui: function import is incomplete')
            return self.summary
        self._check_empty_target()
        mapping = {self.source['id']: self.account['id']}
        writes = 0
        try:
            with zipfile.ZipFile(self.archive) as z:
                for file in self.selected['files']:
                    with z.open('files/' + identifier(file['id'])) as data:
                        meta = file.get('meta') or {}
                        payload = multipart_upload(data, z.getinfo('files/' + file['id']).file_size,
                            meta.get('name') or file.get('filename') or 'upload',
                            meta.get('content_type') or 'application/octet-stream')
                        result = self.app._json('POST', '/api/v1/files/',
                            params={'process': 'false'}, data=payload,
                            headers={'Content-Type': payload.content_type})
                    if not isinstance(result, dict) or not result.get('id'):
                        raise OpenWebUIError('open_webui: file import returned no identifier')
                    new_file = identifier(result['id'])
                    mapping[file['id']] = new_file
                    writes += 1
                    indexed = (file.get('data') or {}).get('content')
                    if isinstance(indexed, str) and indexed:
                        content = self.app._json('POST', f'/api/v1/files/{new_file}/data/content/update',
                                                 json={'content': indexed})
                        if not isinstance(content, dict) or content.get('content') != indexed:
                            raise OpenWebUIError('open_webui: indexed file text was not restored')
                        writes += 1
                    logging.info('open_webui: restored file %s/%s', len(mapping), len(self.selected['files']))
                for knowledge in self.selected['knowledge']:
                    result = self.app._json('POST', '/api/v1/knowledge/create', json={
                        'name': knowledge['name'], 'description': knowledge.get('description') or '', 'access_grants': []})
                    if not isinstance(result, dict) or not result.get('id'):
                        raise OpenWebUIError('open_webui: knowledge import returned no identifier')
                    new_id = identifier(result['id'])
                    mapping[knowledge['id']] = new_id
                    writes += 1
                    for file_id in knowledge['file_ids']:
                        self.app._json('POST', f'/api/v1/knowledge/{new_id}/file/add', json={'file_id': mapping[file_id]})
                        writes += 1
            for tool in self.selected['tools']:
                body = {k: copy.deepcopy(tool[k]) for k in ('id', 'name', 'content', 'meta') if k in tool}
                body['access_grants'] = []
                result = self.app._json('POST', '/api/v1/tools/create', json=body)
                if not isinstance(result, dict) or result.get('id') != tool['id']:
                    raise OpenWebUIError('open_webui: tool import failed')
                writes += 1
                if tool.get('valves') is not None:
                    self.app._json('POST', f'/api/v1/tools/id/{identifier(tool["id"])}/valves/update', json=tool['valves'])
                    writes += 1
            for model in self.selected['models']:
                body = remap(copy.deepcopy(model), mapping, target_prefix=urlsplit(self.app.url).path)
                body.pop('user_id', None)
                body['access_grants'] = []
                result = self.app._json('POST', '/api/v1/models/import', json={'models': [body]})
                if result is not True:
                    raise OpenWebUIError('open_webui: model import failed')
                writes += 1
                restored = self.app._json('GET', '/api/v1/models/model', params={'id': model['id']})
                if (not isinstance(restored, dict) or restored.get('id') != model['id'] or
                        restored.get('user_id') != self.account['id'] or
                        any(restored.get(k) != body.get(k) for k in ('name', 'base_model_id', 'params'))):
                    raise OpenWebUIError('open_webui: model was skipped or changed by the import API')
            for prompt in self.selected['prompts']:
                body = {k: prompt[k] for k in ('command', 'name', 'content', 'data', 'meta', 'tags') if k in prompt}
                body['access_grants'] = []
                result = self.app._json('POST', '/api/v1/prompts/create', json=body)
                if not isinstance(result, dict) or not result.get('id'):
                    raise OpenWebUIError('open_webui: prompt import failed')
                writes += 1
                if prompt.get('is_active') is False:
                    self.app._json('POST', f'/api/v1/prompts/id/{identifier(result["id"])}/toggle')
                    writes += 1
            for chat in self.selected['chats']:
                body = remap({k: chat[k] for k in CHAT_FIELDS if k in chat}, mapping, target_prefix=urlsplit(self.app.url).path)
                result = objects(self.app._json('POST', '/api/v1/chats/import', json={'chats': [body]}), 'restored chats')
                if len(result) != 1 or result[0].get('user_id') != self.account['id']:
                    raise OpenWebUIError('open_webui: chat import returned an unexpected owner')
                writes += 1
            if isinstance(self.source.get('settings'), dict):
                settings = remap(self.source['settings'], mapping, target_prefix=urlsplit(self.app.url).path)
                restored = self.app._json('POST', '/api/v1/users/user/settings/update', json=settings)
                writes += 1
                if not contains_settings(restored, settings):
                    raise OpenWebUIError('open_webui: target permissions discarded some user settings')
        except OpenWebUIError as exc:
            raise OpenWebUIError(f'{exc}. Restore stopped after {writes} successful requests; target may be partially restored. Do not retry into it.') from None
        return self.summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='Preview an Open WebUI API recovery; --apply writes to the target. Restore only trusted archives: tools/functions execute code. See docs/open-webui.md.')
    parser.add_argument('archive')
    parser.add_argument('--url', required=True)
    parser.add_argument('--api-key-file', required=True, help='File containing the target account API key; never pass keys on the command line')
    parser.add_argument('--component', choices=['user', 'functions', 'config'], default='user')
    parser.add_argument('--source-user', help='Original email address; target token must belong to the same email')
    parser.add_argument('--include-shared-resources', action='store_true', help='Allow private copies of referenced files/knowledge owned by others after manually verifying source sharing; ACLs are not verified automatically')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    try:
        key = Path(args.api_key_file).read_text().strip()
        restore = OpenWebUIRestore(OpenWebUIApp(args.url, key), args.archive,
                                   component=args.component, source_user=args.source_user,
                                   include_shared_resources=args.include_shared_resources)
        print(json.dumps(restore.plan(), indent=2))
        if args.apply:
            restore.apply()
            print('Restore completed. Recreate sharing permissions and review the documented exclusions.')
        else:
            print('Preview only. Add --apply to restore into the prepared target.')
    except (OpenWebUIError, OSError) as exc:
        print(str(exc) if isinstance(exc, OpenWebUIError) else 'open_webui: could not read local input', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
