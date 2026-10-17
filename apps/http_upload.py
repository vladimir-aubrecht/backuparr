"""Multipart bodies with a known length and bounded file reads."""
from requests import RequestException
from requests_toolbelt.multipart.encoder import MultipartEncoder


class SizedStream:
    def __init__(self, stream, length):
        self.stream, self.len = stream, length

    def read(self, size=-1):
        # MultipartEncoder/urllib3 normally ask for small chunks; also bound
        # direct read() calls so the wrapper never reads a whole archive.
        size = min(size if size is not None and size >= 0 else 64 * 1024, 64 * 1024, self.len)
        data = self.stream.read(size)
        if not data and self.len:
            raise RequestException("Multipart upload source ended early")
        self.len -= len(data)
        return data


def multipart_body(files, data=None):
    """files maps a field to (filename, stream, remaining size, content type)."""
    fields = dict(data or {})
    for field, (name, stream, size, content_type) in files.items():
        fields[field] = (name, SizedStream(stream, size), content_type)
    return MultipartEncoder(fields=fields)
