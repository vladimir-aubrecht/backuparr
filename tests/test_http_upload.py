"""Prepared HTTP requests must stream multipart files instead of buffering them."""
import io
from email import policy
from email.parser import BytesParser

import pytest
import requests

from apps.http_upload import multipart_body


def read_multipart(body, content_type):
    chunks = []
    while chunk := body.read(16384):
        chunks.append(chunk)
    message = BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + b''.join(chunks))
    files, fields = {}, {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        content = part.get_payload(decode=True)
        if part.get_filename() is not None:
            files[name] = (part.get_filename(), content, part.get_content_type())
        else:
            fields[name] = content.decode()
    return files, fields


class BoundedFile(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.reads = []

    def read(self, size=-1):
        assert 0 <= size <= 65536, 'An upload attempted to read the entire file'
        self.reads.append(size)
        return super().read(size)


def test_prepared_request_streams_multiple_files_and_keeps_content_length():
    content = bytes(range(256)) * 1200
    source = BoundedFile(content)
    second = BoundedFile(b'thumbnail')
    body = multipart_body({
        'saveFile': ('save.zip', source, len(content), 'application/zip'),
        'screenshotFile': ('image.png', second, 9, 'image/png'),
    }, {'name': 'Fixture', 'rom_ids': '[12, 34]'})
    request = requests.Request('POST', 'https://fixture.example/upload', data=body,
        headers={'Content-Type': body.content_type, 'Authorization': 'Bearer fixture'}).prepare()
    assert source.reads == second.reads == []
    assert request.headers['Content-Length'] == str(body.len)
    assert 'Transfer-Encoding' not in request.headers
    files, fields = read_multipart(request.body, request.headers['Content-Type'])
    assert files == {'saveFile': ('save.zip', content, 'application/zip'),
                     'screenshotFile': ('image.png', b'thumbnail', 'image/png')}
    assert fields == {'name': 'Fixture', 'rom_ids': '[12, 34]'}
    assert len(source.reads) > 1


def test_truncated_upload_does_not_loop_forever_or_report_success():
    body = multipart_body({'file': ('backup.zip', io.BytesIO(b'short'), 100, 'application/zip')})
    with pytest.raises(requests.RequestException, match='ended early'):
        read_multipart(body, body.content_type)
