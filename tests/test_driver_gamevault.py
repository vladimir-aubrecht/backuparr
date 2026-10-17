"""Fixtures model GameVault 17.0.1's native controllers (see README sources)."""
import io
import json
import sqlite3
import tarfile
from pathlib import Path
import zipfile

import pytest
import requests

from apps.gamevault import GamevaultApp, GamevaultError, _database_contents

KEY = "source-admin-key"
PASSWORD = "database-private-password"


def database(tmp_path, name="source", value="played"):
    path = tmp_path / (name + ".db")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE progress(id INTEGER, state TEXT)")
        db.execute("INSERT INTO progress VALUES (1, ?)", (value,))
    return path.read_bytes()


def save_zip():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("save.dat", b"game progress")
    return output.getvalue()


class Reply:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code = status
        self.data = data
        self.content = content
        self.closed = False

    def json(self):
        if self.data is None:
            raise ValueError()
        return self.data

    def iter_content(self, size):
        yield self.content

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Session:
    def __init__(self, db, key=KEY):
        self.database, self.key = db, key
        self.sent = []
        self.writes = []
        self.saves = {(1, 10): save_zip()}
        self.news = b"# Welcome"
        self.version = "17.0.1"
        self.ignore_restore = False

    def request(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert kwargs["headers"]["X-Api-Key"] == self.key
        assert KEY not in url and PASSWORD not in url
        route = url.split("/api", 1)[1]
        self.sent.append((method, route, kwargs))
        if route.startswith("/admin/database/"):
            assert kwargs["headers"]["X-Database-Password"] == PASSWORD
        else:
            assert "X-Database-Password" not in kwargs["headers"]
        if method == "GET":
            if route == "/users/me":
                return Reply(data={"id": 1, "role": 3})
            if route == "/status":
                return Reply(data={"version": self.version})
            if route == "/users":
                return Reply(data=[{"id": 1}, {"id": 2}])
            if route == "/games":
                assert kwargs["params"] == {"page": 1, "limit": 100, "sortBy": "id:ASC"}
                return Reply(data={"data": [{"id": 10}], "meta": {"totalItems": 1, "currentPage": 1}})
            if route == "/admin/database/backup":
                return Reply(content=self.database)
            if route == "/config/news":
                return Reply(content=self.news) if self.news is not None else Reply(404)
            if route.startswith("/savefiles/"):
                key = tuple(map(int, route.split("/")[3::2]))
                return Reply(content=self.saves[key]) if key in self.saves else Reply(404)
        if method == "POST" and route == "/admin/database/restore":
            filename, stream, mime = kwargs["files"]["file"]
            assert filename == "database.db" and mime == "application/octet-stream"
            value = stream.read()
            self.writes.append((route, value))
            if not self.ignore_restore:
                self.database = value
                self.key = KEY
            return Reply(201)
        if method == "POST" and route.startswith("/savefiles/"):
            filename, stream, mime = kwargs["files"]["file"]
            assert filename == "save.zip" and mime == "application/zip"
            self.writes.append((route, stream.read()))
            return Reply(201)
        if method == "PUT" and route == "/config/news":
            self.writes.append((route, kwargs["json"]["content"]))
            return Reply(200)
        raise AssertionError((method, route))


def driver(db, key=KEY):
    app = GamevaultApp("http://gamevault:8080/base/api/", key, PASSWORD)
    app.session = Session(db, key)
    # Fixture routes are relative to the configured reverse proxy base path.
    return app


def test_backup_and_restore_full_database_news_and_latest_saves(tmp_path):
    source = driver(database(tmp_path))
    output = source.backup(tmp_path / "backup")
    assert Path(output).stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"database.db", "news.md", "savefiles/1/10.zip", "manifest.json"}
        assert archive.read("database.db") == source.session.database
        assert PASSWORD.encode() not in archive.read("manifest.json")
    target = driver(database(tmp_path, "target", "unplayed"), "target-admin-key")
    result = target.restore(output)
    assert result == {"database": "table contents verified", "news": True, "savefiles": 1}
    assert target.session.database == source.session.database
    assert [p for p, _ in target.session.writes] == ["/admin/database/restore", "/config/news", "/savefiles/user/1/game/10"]
    assert target.api_key == KEY
    assert target.session.writes[-1][1] == save_zip()


def test_test_connection_does_not_download_or_restore_database(tmp_path):
    app = driver(database(tmp_path))
    assert "DB_PASSWORD is checked during backup" in app.test_connection()
    assert [route for _, route, _ in app.session.sent] == ["/users/me", "/status"]


@pytest.mark.parametrize("url", ["ftp://host", "http://user:secret@host", "http://host?key=secret", "http://host#secret", "garbage"])
def test_rejects_unsafe_urls(url):
    with pytest.raises(GamevaultError):
        GamevaultApp(url, KEY, PASSWORD)


@pytest.mark.parametrize("key,password", [("", PASSWORD), (KEY, "")])
def test_requires_both_credentials(key, password):
    with pytest.raises(GamevaultError, match="required"):
        GamevaultApp("http://host", key, password)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 500])
def test_http_failures_hide_secrets_and_never_follow_redirects(tmp_path, status):
    app = driver(database(tmp_path))
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return Reply(status, data={"message": KEY + PASSWORD})
    app.session.request = request
    with pytest.raises(GamevaultError) as error:
        app.backup(tmp_path / "backup")
    assert KEY not in str(error.value) and PASSWORD not in str(error.value)
    assert not (tmp_path / "backup/gamevault-backup.zip").exists()
    assert len(calls) == 1 and not calls[0]["allow_redirects"]


def test_network_failure_hides_credentials(tmp_path):
    app = driver(database(tmp_path))
    def fail(*a, **kw):
        raise requests.ConnectionError(KEY + PASSWORD)
    app.session.request = fail
    with pytest.raises(GamevaultError) as error:
        app.test_connection()
    assert KEY not in str(error.value) and PASSWORD not in str(error.value)
    assert error.value.__suppress_context__


def test_wrong_version_stops_before_restore(tmp_path):
    app = driver(database(tmp_path))
    output = app.backup(tmp_path / "backup")
    app.session.version = "18.0.0"
    with pytest.raises(GamevaultError, match="same GameVault version"):
        app.restore(output)
    assert not app.session.writes


def test_silent_upstream_restore_failure_is_detected_before_other_writes(tmp_path):
    app = driver(database(tmp_path))
    output = app.backup(tmp_path / "backup")
    app.session.database = database(tmp_path, "target", "different")
    app.session.ignore_restore = True
    with pytest.raises(GamevaultError, match="verification failed"):
        app.restore(output)
    assert [route for route, _ in app.session.writes] == ["/admin/database/restore"]


def test_corrupt_archive_stops_before_any_target_request(tmp_path):
    app = driver(database(tmp_path))
    output = app.backup(tmp_path / "backup")
    with zipfile.ZipFile(output, "a") as archive:
        archive.writestr("../outside", b"unsafe")
    app.session.sent.clear()
    with pytest.raises(GamevaultError, match="nothing restored"):
        app.restore(output)
    assert not app.session.sent


def test_missing_optional_news_and_cloud_saves_are_documented(tmp_path):
    app = driver(database(tmp_path))
    app.session.news, app.session.saves = None, {}
    output = app.backup(tmp_path / "backup")
    assert app.restore(output) == {"database": "table contents verified", "news": False, "savefiles": 0}


def test_rejects_html_database_download(tmp_path):
    app = driver(b"<html>Login</html>")
    with pytest.raises(GamevaultError, match="database dump"):
        app.backup(tmp_path / "backup")
    assert not (tmp_path / "backup/gamevault-backup.zip").exists()


def test_broken_savefile_aborts_backup(tmp_path):
    app = driver(database(tmp_path))
    app.session.saves[(1, 10)] = b"<html>Login</html>"
    with pytest.raises(GamevaultError, match="valid ZIP"):
        app.backup(tmp_path / "backup")
    assert not (tmp_path / "backup/gamevault-backup.zip").exists()


def test_paginated_games_ignore_untrusted_next_links(tmp_path):
    app = driver(database(tmp_path))
    pages = []
    def get(path, **kwargs):
        page = kwargs["params"]["page"]
        pages.append(page)
        return {"data": [{"id": page}], "meta": {"totalItems": 2, "currentPage": page},
                "links": {"next": "https://attacker.invalid"}}
    app._json = get
    assert app._games() == [1, 2] and pages == [1, 2]


def test_incomplete_pagination_fails(tmp_path):
    app = driver(database(tmp_path))
    app._json = lambda *a, **k: {"data": [], "meta": {"totalItems": 2, "currentPage": 1}}
    with pytest.raises(GamevaultError, match="pagination"):
        app._games()


def postgres_tar(path, order, filename="123.dat"):
    script = f"COPY public.progress (id, state) FROM stdin;\n\\.\n\nCOPY public.progress (id, state) FROM '$$PATH$$/{filename}';\nSELECT pg_catalog.setval('public.seq', 2, true);\n"
    with tarfile.open(path, "w") as archive:
        for name, data in {"toc.dat": b"PGDMPtimestamp", "restore.sql": script.encode(), filename: order}.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def test_postgresql_contents_ignore_tar_ids_and_table_row_order(tmp_path):
    first, second = tmp_path / "first.tar", tmp_path / "second.tar"
    postgres_tar(first, b"1\tplayed\n2\tunplayed\n\\.\n")
    postgres_tar(second, b"2\tunplayed\n1\tplayed\n\\.\n", "456.dat")
    assert _database_contents(first) == _database_contents(second)
    assert _database_contents(first)[0] == "postgresql"


def test_factory_and_encrypted_db_password(authed_client):
    import config_store
    import backup
    cfg = {"enabled": True, "url": "http://gamevault:8080", "api_key": KEY, "database_password": PASSWORD}
    response = authed_client.post("/api/config", json={"apps": {"gamevault": cfg}})
    assert response.status_code == 200
    persisted = Path(config_store.CONFIG_PATH).read_text()
    assert PASSWORD not in persisted and KEY not in persisted
    assert config_store.load_config()["apps"]["gamevault"]["database_password"] == PASSWORD
    assert isinstance(backup.build_app("gamevault", cfg), GamevaultApp)
    assert "gamevault" in authed_client.get("/").get_data(as_text=True)


def test_gamevault_restore_is_wired_to_shared_action(monkeypatch, tmp_path):
    import restore_actions
    monkeypatch.setattr(GamevaultApp, "restore", lambda self, path: {"database": "verified"})
    result = restore_actions.restore_app("gamevault", {"url": "http://host", "api_key": KEY, "database_password": PASSWORD}, str(tmp_path), "backup.zip")
    assert result == {"kind": "gamevault", "summary": {"database": "verified"}}


def test_disabled_cloud_saves_still_back_up_database(tmp_path, caplog):
    app = driver(database(tmp_path))
    original = app.session.request
    def disabled(method, url, **kwargs):
        if "/savefiles/" in url:
            return Reply(405)
        return original(method, url, **kwargs)
    app.session.request = disabled
    with caplog.at_level("INFO"):
        output = app.backup(tmp_path / "backup")
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["saves"] == [] and manifest["savefiles_enabled"] is False
    assert "cloud saves excluded" in caplog.text


def test_restore_requires_save_api_before_database_write(tmp_path):
    app = driver(database(tmp_path))
    output = app.backup(tmp_path / "backup")
    original = app.session.request
    def disabled(method, url, **kwargs):
        if "/savefiles/" in url:
            return Reply(405)
        return original(method, url, **kwargs)
    app.session.request = disabled
    with pytest.raises(GamevaultError, match="SAVEFILES_ENABLED"):
        app.restore(output)
    assert not app.session.writes


def test_empty_news_is_excluded_because_gamevault_rejects_empty_put(tmp_path, caplog):
    app = driver(database(tmp_path))
    app.session.news = b""
    with caplog.at_level("INFO"):
        output = app.backup(tmp_path / "backup")
    with zipfile.ZipFile(output) as archive:
        assert "news.md" not in archive.namelist()
    assert "empty news page omitted" in caplog.text
    assert app.restore(output)["news"] is False


def test_database_changes_during_export_abort_backup(tmp_path):
    app = driver(database(tmp_path))
    changed = database(tmp_path, "changed", "played-more")
    original = app.session.request
    dumps = 0
    def changing(method, url, **kwargs):
        nonlocal dumps
        if url.endswith("/admin/database/backup"):
            dumps += 1
            if dumps == 2:
                return Reply(content=changed)
        return original(method, url, **kwargs)
    app.session.request = changing
    with pytest.raises(GamevaultError, match="changed during backup"):
        app.backup(tmp_path / "backup")
    assert not (tmp_path / "backup/gamevault-backup.zip").exists()


def test_restore_rejects_wrong_database_engine_before_any_write(tmp_path):
    app = driver(database(tmp_path))
    output = app.backup(tmp_path / "backup")
    postgres_tar(tmp_path / "pg.tar", b"1\tplayed\n\\.\n")
    app.session.database = (tmp_path / "pg.tar").read_bytes()
    with pytest.raises(GamevaultError, match="engines must match"):
        app.restore(output)
    assert not app.session.writes


def test_native_postgresql_dump_restore_fixtures(tmp_path):
    import gzip
    contents = []
    for name in ("source", "target"):
        fixture = Path(__file__).parent / "fixtures/gamevault" / f"postgres17-{name}.tar.gz"
        path = tmp_path / f"{name}.tar"
        path.write_bytes(gzip.decompress(fixture.read_bytes()))
        contents.append(_database_contents(path))
    assert contents[0] == contents[1]
    assert contents[0][0] == "postgresql"
    assert len(contents[0][1]) == 4  # three tables plus sequence values


@pytest.mark.parametrize("bad_preflight", [False, True])
def test_existing_backup_is_never_deleted(tmp_path, bad_preflight):
    output = tmp_path / "gamevault-backup.zip"
    output.write_bytes(b"existing valid backup")
    app = driver(b"invalid database" if bad_preflight else database(tmp_path))
    with pytest.raises((GamevaultError, FileExistsError)):
        app.backup(tmp_path)
    assert output.read_bytes() == b"existing valid backup"
