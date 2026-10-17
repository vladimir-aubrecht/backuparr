"""GameVault's native database backup/restore plus news and latest cloud saves.

API contracts: Phalcode/gamevault-backend 859d71ef (17.0.1), documented in
README. All network requests stay on the configured API origin. A database
restore replaces user credentials as well as data, so subsequent requests use
the backed-up administrator key. No database statements are executed locally.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tarfile
import tempfile
from urllib.parse import urlsplit
import zipfile

import requests

from apps.http_upload import multipart_body

log = logging.getLogger(f"backuparr.{__name__}")


class GamevaultError(RuntimeError):
    pass


class _SavefilesDisabled(GamevaultError):
    pass


def _hash_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _database_contents(path):
    """Engine and logical table contents, independent of dump timestamps/order.

    PostgreSQL's tar archive provides COPY table/column names in restore.sql.
    Never run that script or extract paths from it. Include sequence values.
    SQLite is opened immutable/read-only; no SQL from the backup is executed.
    """
    with open(path, "rb") as stream:
        sqlite = stream.read(16) == b"SQLite format 3\0"
    try:
        if sqlite:
            with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as db:
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise GamevaultError("gamevault: invalid SQLite database")
                tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                result = {}
                for (name,) in tables:
                    quoted = '"' + name.replace('"', '""') + '"'
                    rows = db.execute(f"SELECT * FROM {quoted}")
                    # repr distinguishes bytes, strings, nulls and numeric values.
                    result[name] = sorted(hashlib.sha256(repr(tuple(row)).encode()).hexdigest() for row in rows)
                if not result:
                    raise GamevaultError("gamevault: database contains no tables")
                return "sqlite", result
        with tarfile.open(path, "r:") as archive:
            members = archive.getmembers()
            names = [m.name for m in members]
            if len(names) != len(set(names)) or any(not m.isfile() for m in members):
                raise GamevaultError("gamevault: invalid PostgreSQL dump members")
            toc = archive.extractfile("toc.dat")
            if toc is None or toc.read(5) != b"PGDMP":
                raise GamevaultError("gamevault: invalid PostgreSQL dump")
            script = archive.extractfile("restore.sql").read().decode("utf-8")
            references = re.findall(r"(COPY .+?) FROM '\$\$PATH\$\$/([^']+)';", script, re.S)
            result = {}
            for statement, filename in references:
                # A preceding COPY ... FROM stdin block can appear in restore.sql.
                statement = statement.rsplit("\nCOPY ", 1)[-1]
                statement = statement.removeprefix("COPY ")
                data = archive.extractfile(filename)
                if data is None:
                    raise GamevaultError("gamevault: incomplete PostgreSQL dump")
                result[statement] = sorted(hashlib.sha256(row).hexdigest() for row in data)
            referenced = {filename for _, filename in references}
            if not references or any(name.endswith(".dat") and name != "toc.dat" and name not in referenced for name in names):
                raise GamevaultError("gamevault: unsupported PostgreSQL dump layout")
            result["__sequences__"] = sorted(re.findall(r"SELECT pg_catalog\.setval\([^\n]+", script))
            return "postgresql", result
    except (sqlite3.Error, tarfile.TarError, KeyError, AttributeError, UnicodeError, ValueError):
        raise GamevaultError("gamevault: invalid or unsupported database dump") from None


class GamevaultApp:
    def __init__(self, url, api_key, database_password="", timeout=30):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GamevaultError("gamevault: use an HTTP(S) server URL without credentials, query or fragment")
        if not api_key or not database_password:
            raise GamevaultError("gamevault: an administrator API key and GameVault DB_PASSWORD are required")
        self.url = url.rstrip("/")
        if self.url.endswith("/api"):
            self.url = self.url[:-4]
        self.api_key, self.database_password = api_key, database_password
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method, path, *, database=False, optional=False, **kwargs):
        headers = {"X-Api-Key": self.api_key}
        if database:
            headers["X-Database-Password"] = self.database_password
        files = kwargs.pop("files", None)
        if files:
            payload = multipart_body({
                name: (filename, stream, os.fstat(stream.fileno()).st_size - stream.tell(), mime)
                for name, (filename, stream, mime) in files.items()
            })
            kwargs["data"] = payload
            headers["Content-Type"] = payload.content_type
        try:
            response = self.session.request(method, self.url + "/api" + path, headers=headers,
                                            timeout=kwargs.pop("timeout", self.timeout), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise GamevaultError("gamevault: API connection failed; check connectivity and server logs") from None
        if optional and response.status_code == 404:
            response.close()
            return None
        if response.status_code == 405 and path.startswith("/savefiles/"):
            response.close()
            raise _SavefilesDisabled("gamevault: cloud-save API is disabled; enable SAVEFILES_ENABLED before restoring saves")
        if response.status_code not in (200, 201, 204):
            status = response.status_code
            response.close()
            if 300 <= status < 400:
                raise GamevaultError("gamevault: redirect refused; use the final server URL")
            if status in (401, 403):
                raise GamevaultError("gamevault: access denied; check administrator API key and DB_PASSWORD")
            raise GamevaultError(f"gamevault: API returned HTTP {status}")
        return response

    def _json(self, path, **kwargs):
        with self._request("GET", path, **kwargs) as response:
            try:
                return response.json()
            except ValueError:
                raise GamevaultError("gamevault: API returned invalid JSON") from None

    def _identity(self):
        user = self._json("/users/me")
        status = self._json("/status")
        if not isinstance(user, dict) or user.get("role") != 3 or not isinstance(user.get("id"), int):
            raise GamevaultError("gamevault: API key must belong to an administrator")
        if not isinstance(status, dict) or not isinstance(status.get("version"), str):
            raise GamevaultError("gamevault: server version unavailable")
        return user, status["version"]

    def test_connection(self):
        _, version = self._identity()
        # No cheap API validates DB_PASSWORD: testing must not download a dump.
        return f"gamevault {version} reachable (DB_PASSWORD is checked during backup)"

    def _download(self, route, path, **kwargs):
        response = self._request("GET", route, stream=True, timeout=600, **kwargs)
        if response is None:
            return False
        try:
            with response, open(path, "xb") as target:
                os.chmod(path, 0o600)
                for chunk in response.iter_content(1024 * 1024):
                    target.write(chunk)
        except requests.RequestException:
            raise GamevaultError("gamevault: download interrupted; backup is incomplete") from None
        return True

    def _games(self):
        seen, page, total = set(), 1, None
        while True:
            result = self._json("/games", params={"page": page, "limit": 100, "sortBy": "id:ASC"})
            try:
                rows, meta = result["data"], result["meta"]
                count = meta["totalItems"]
                if not isinstance(rows, list) or type(count) is not int or count < 0 or meta["currentPage"] != page:
                    raise ValueError()
                if total is not None and count != total:
                    raise ValueError()
                total = count
                for item in rows:
                    ident = item["id"]
                    if type(ident) is not int or ident <= 0 or ident in seen:
                        raise ValueError()
                    seen.add(ident)
                if len(seen) == total:
                    return sorted(seen)
                if not rows or len(seen) > total:
                    raise ValueError()
                page += 1
            except (KeyError, TypeError, ValueError):
                raise GamevaultError("gamevault: game list changed or pagination is incomplete; retry when idle") from None

    def backup(self, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        output = os.path.join(dest_dir, "gamevault-backup.zip")
        created = False
        try:
            with tempfile.TemporaryDirectory(prefix="gamevault-") as work:
                admin, version = self._identity()
                db = os.path.join(work, "database.db")
                self._download("/admin/database/backup", db, database=True)
                engine, source_contents = _database_contents(db)
                log.info("gamevault: database downloaded and validated (%s)", engine)
                files = {"database.db": db}
                news = os.path.join(work, "news.md")
                if self._download("/config/news", news, optional=True):
                    # Validate before creating a backup that could fail mid-restore.
                    try:
                        news_text = Path(news).read_text(encoding="utf-8")
                    except UnicodeError:
                        raise GamevaultError("gamevault: news.md is not UTF-8") from None
                    if news_text:
                        files["news.md"] = news
                    else:
                        log.info("gamevault: empty news page omitted (GameVault rejects restoring empty content)")
                users = self._json("/users")
                if not isinstance(users, list) or any(not isinstance(u, dict) or type(u.get("id")) is not int or u["id"] <= 0 for u in users):
                    raise GamevaultError("gamevault: invalid user list")
                user_ids = [u["id"] for u in users]
                if len(user_ids) != len(set(user_ids)) or admin["id"] not in user_ids:
                    raise GamevaultError("gamevault: incomplete user list")
                games = self._games()
                saves = []
                saves_disabled = False
                for uid in user_ids:
                    if saves_disabled:
                        break
                    for gid in games:
                        name = f"savefiles/{uid}/{gid}.zip"
                        local = os.path.join(work, f"save-{uid}-{gid}.zip")
                        try:
                            downloaded = self._download(f"/savefiles/user/{uid}/game/{gid}", local, optional=True)
                        except _SavefilesDisabled:
                            saves_disabled = True
                            log.info("gamevault: cloud saves excluded because the savefile API is disabled (HTTP 405)")
                            break
                        if downloaded:
                            try:
                                with zipfile.ZipFile(local) as archive:
                                    if archive.testzip() is not None:
                                        raise zipfile.BadZipFile()
                            except zipfile.BadZipFile:
                                raise GamevaultError("gamevault: savefile download is not a valid ZIP") from None
                            files[name] = local
                            saves.append({"user_id": uid, "game_id": gid, "file": name})
                    log.info("gamevault: latest saves checked for user %d (%d games)", uid, len(games))
                checked_db = os.path.join(work, "checked.db")
                self._download("/admin/database/backup", checked_db, database=True)
                if _database_contents(checked_db) != (engine, source_contents):
                    raise GamevaultError("gamevault: database changed during backup; pause clients/indexing and retry")
                manifest = {"format": "backuparr-gamevault", "version": 1, "server_version": version,
                            "engine": engine, "admin_id": admin["id"], "restore_api_key": self.api_key,
                            "saves": saves, "savefiles_enabled": not saves_disabled, "files": {name: _hash_file(path) for name, path in files.items()}}
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(fd, "wb") as stream:
                    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                        for name, path in files.items():
                            archive.write(path, name)
                        archive.writestr("manifest.json", json.dumps(manifest))
                log.info("gamevault: backup complete: database, %snews, %d latest saves; media files and server environment excluded",
                         "" if "news.md" in files else "no ", len(saves))
                return output
        except BaseException:
            if created:
                os.unlink(output)
            raise

    def restore(self, archive_path):
        with tempfile.TemporaryDirectory(prefix="gamevault-restore-") as work:
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    if not isinstance(manifest, dict) or manifest.get("format") != "backuparr-gamevault" or manifest.get("version") != 1:
                        raise ValueError()
                    files, saves = manifest["files"], manifest["saves"]
                    expected = {"database.db"} | ({"news.md"} if "news.md" in files else set())
                    if not isinstance(saves, list) or not isinstance(manifest["restore_api_key"], str) or not manifest["restore_api_key"]:
                        raise ValueError()
                    for save in saves:
                        uid, gid = save["user_id"], save["game_id"]
                        if type(uid) is not int or type(gid) is not int or min(uid, gid) <= 0 or save["file"] != f"savefiles/{uid}/{gid}.zip" or save["file"] in expected:
                            raise ValueError()
                        expected.add(save["file"])
                    if set(files) != expected or set(archive.namelist()) != expected | {"manifest.json"} or len(archive.namelist()) != len(expected) + 1:
                        raise ValueError()
                    paths = {}
                    for index, name in enumerate(sorted(expected)):
                        path = os.path.join(work, f"file-{index}")
                        with archive.open(name) as source, open(path, "xb") as target:
                            while chunk := source.read(1024 * 1024):
                                target.write(chunk)
                        if _hash_file(path) != files[name]:
                            raise ValueError()
                        paths[name] = path
                    news = Path(paths["news.md"]).read_text(encoding="utf-8") if "news.md" in paths else None
                    if news == "":
                        raise ValueError()
                    for save in saves:
                        with zipfile.ZipFile(paths[save["file"]]) as saved:
                            if saved.testzip() is not None:
                                raise ValueError()
            except (zipfile.BadZipFile, KeyError, TypeError, ValueError, UnicodeError):
                raise GamevaultError("gamevault: invalid, incomplete or corrupt backup; nothing restored") from None
            engine, contents = _database_contents(paths["database.db"])
            if engine != manifest["engine"]:
                raise GamevaultError("gamevault: backup database engine does not match manifest")
            target_admin, version = self._identity()
            if version != manifest["server_version"]:
                raise GamevaultError("gamevault: restore to the same GameVault version first, then upgrade")
            if saves:
                probe = self._request("GET", f"/savefiles/user/{target_admin['id']}/game/{saves[0]['game_id']}", optional=True, stream=True)
                if probe is not None:
                    probe.close()
            target_db = os.path.join(work, "target.db")
            self._download("/admin/database/backup", target_db, database=True)
            if _database_contents(target_db)[0] != engine:
                raise GamevaultError("gamevault: source and target database engines must match")
            log.info("gamevault: restoring database (replaces all users and credentials)...")
            with open(paths["database.db"], "rb") as source:
                self._request("POST", "/admin/database/restore", database=True, timeout=600,
                              files={"file": ("database.db", source, "application/octet-stream")}).close()
            self.api_key = manifest["restore_api_key"]
            restored = os.path.join(work, "verified.db")
            self._download("/admin/database/backup", restored, database=True)
            if _database_contents(restored) != (engine, contents):
                raise GamevaultError("gamevault: database restore verification failed; inspect GameVault logs before retrying")
            log.info("gamevault: restored database table contents verified")
            if news is not None:
                self._request("PUT", "/config/news", json={"content": news}).close()
                log.info("gamevault: news restored")
            for save in saves:
                with open(paths[save["file"]], "rb") as source:
                    self._request("POST", f"/savefiles/user/{save['user_id']}/game/{save['game_id']}", timeout=600,
                                  files={"file": ("save.zip", source, "application/zip")}).close()
                log.info("gamevault: save restored for user %d, game %d", save["user_id"], save["game_id"])
            return {"database": "table contents verified", "news": news is not None, "savefiles": len(saves)}
