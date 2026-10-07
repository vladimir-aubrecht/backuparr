"""Export NZBGet's saved configuration through its authenticated JSON-RPC API.

loadconfig preserves variable references and, with the full-access control
account, passwords. Queue/history files and extension scripts are not exported.
"""
import os
from urllib.parse import urlsplit

import requests


class NzbgetError(RuntimeError):
    pass


class NzbgetApp:
    def __init__(self, url, username, password, timeout=30):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None and not parsed.query and not parsed.fragment
                     and parsed.port != 0)
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise NzbgetError("nzbget: use an http(s) server URL without credentials or a query string")
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            raise NzbgetError("nzbget: Control username and Control password are required")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (username, password)

    def _rpc(self, method, params=None):
        try:
            response = self.session.post(
                f"{self.url}/jsonrpc",
                json={"method": method, "params": params or [], "id": 1},
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException:
            raise NzbgetError("nzbget: could not connect; check the server URL and network access") from None
        with response:
            if response.status_code in (401, 403):
                raise NzbgetError("nzbget: access denied; use the full-access Control username and password")
            if 300 <= response.status_code < 400:
                raise NzbgetError("nzbget: RPC request was redirected; use the final server URL or an internal URL")
            if response.status_code != 200:
                raise NzbgetError(f"nzbget: {method} failed (HTTP {response.status_code})")
            try:
                payload = response.json()
            except ValueError:
                raise NzbgetError(f"nzbget: {method} did not return a JSON-RPC response") from None
        if not isinstance(payload, dict) or payload.get("error") is not None or payload.get("id") != 1:
            raise NzbgetError(f"nzbget: {method} was rejected; check full-access RPC permissions")
        return payload.get("result")

    def _config(self):
        rows = self._rpc("loadconfig")
        if not isinstance(rows, list) or not rows:
            raise NzbgetError("nzbget: loadconfig returned no usable configuration")
        names = set()
        for row in rows:
            if not isinstance(row, dict):
                raise NzbgetError("nzbget: invalid configuration response")
            name, value = row.get("Name"), row.get("Value")
            if (not isinstance(name, str) or not name or name != name.strip()
                    or any(c in name for c in "\r\n\x00=") or name.startswith("#")
                    or not isinstance(value, str) or any(c in value for c in "\r\n\x00")
                    or name.casefold() in names):
                raise NzbgetError("nzbget: configuration cannot be represented safely as an NZBGet config file")
            if value == "***":
                raise NzbgetError("nzbget: configuration contains masked values; use the full-access Control account, not Restricted or Add credentials")
            names.add(name.casefold())
        return rows

    def test_connection(self):
        self._config()
        return "nzbget reachable; saved configuration and credentials are readable"

    def backup(self, dest_dir):
        rows = self._config()
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, "nzbget.conf")
        with open(path, "w", encoding="utf-8", newline="\n") as output:
            os.chmod(path, 0o600)
            output.write("# NZBGet configuration exported by Backuparr using loadconfig.\n")
            for row in rows:
                output.write(f"{row['Name']}={row['Value']}\n")
        with open(os.path.join(dest_dir, "RESTORE.txt"), "w", encoding="utf-8") as output:
            output.write(
                "NZBGet configuration backup\n\n"
                "Contains saved settings, including server and control passwords.\n"
                "Does not contain queued NZB contents/articles, downloaded files, or extension scripts.\n"
                "Restore using Settings > System > Restore settings in NZBGet, or stop\n"
                "NZBGet and replace its configuration file with nzbget.conf, preserving\n"
                "file ownership and permissions. Review paths before starting NZBGet.\n"
                "Reinstall extension scripts separately. Queue/history/statistics are not\n"
                "exported: the API cannot restore these native state files.\n"
            )
        return dest_dir
