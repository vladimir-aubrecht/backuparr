"""API-shape regression fixtures based on the official RomM 5.4.0 routes.

These exercise real export/import code and ZIP bytes, not a running RomM server.
"""
import copy
import io
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

import pytest
import requests

from apps.romm import EXCLUSIONS, RommApp, RommError, portable_metadata
from apps.romm_restore import RommRestore, match_roms


def config():
    return {"CONFIG_FILE_WRITABLE": True, "SCAN_METADATA_PRIORITY": ["igdb"],
        "SCAN_ARTWORK_PRIORITY": ["ss"], "SCAN_REGION_PRIORITY": ["us"],
        "SCAN_LANGUAGE_PRIORITY": ["en"], "SCAN_MEDIA": ["cover"],
        "GAMELIST_AUTO_EXPORT_ON_SCAN": False, "GAMELIST_MEDIA_THUMBNAIL": "cover",
        "GAMELIST_MEDIA_IMAGE": "screenshot", "PEGASUS_AUTO_EXPORT_ON_SCAN": False,
        "SCAN_ARTWORK_PRIORITY_OVERRIDES": {}, "PLATFORMS_BINDING": {"snes": "snes"},
        "PLATFORMS_VERSIONS": {}, "CONVERTO": {"download_conversion_enabled": False,
        "cache_max_size_gb": 20, "platform_formats": {}}, **{k: [] for k in EXCLUSIONS}}


class Reply:
    def __init__(self, data=None, status=200, content=b"binary fixture"):
        self.data, self.status_code, self.content = data, status, content
        self.headers = {"Content-Type": "application/octet-stream"}

    def json(self):
        return copy.deepcopy(self.data)

    def iter_content(self, size):
        yield self.content

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Server:
    def __init__(self, target=False):
        self.headers, self.calls = {}, []
        self.user = {"id": 11 if target else 1, "username": "admin", "role": "admin"}
        self.rid = 110 if target else 10
        self.pid = 120 if target else 20
        self.cid = 130 if target else 30
        self.cfg = config()
        self.rom = {"id": self.rid, "platform_slug": "snes", "platform_id": self.pid,
            "fs_path": "roms/snes", "fs_name": "Mario.sfc", "sha1_hash": "a" * 40,
            "name": "Mario", "name_sort_key": "mario", "summary": "Personal description",
            "manual_metadata": {"genres": ["Platform"]}, "igdb_id": 123,
            "igdb_metadata": {"name": "Mario", "image_path": "old/20/10.jpg"},
            "rom_user": {"is_main_sibling": False, "backlogged": True, "now_playing": False,
                "hidden": False, "rating": 8, "difficulty": 5, "completion": 100, "status": "completed_100"},
            "user_screenshots": [], "path_cover_large": "/assets/romm/resources/roms/20/10/cover.jpg?ts=123",
            "path_manual": "roms/20/10/manual/10.pdf"}
        self.collection = {"id": self.cid, "user_id": self.user["id"], "owner_username": "admin",
            "name": "Favorites", "description": "my games", "is_public": False, "is_favorite": True,
            "rom_ids": [self.rid]}
        self.smart = {"id": 140 if target else 40, "user_id": self.user["id"],
            "name": "Completed", "description": "finished games", "is_public": False,
            "filter_criteria": {"platform_ids": [self.pid], "collection_id": self.cid,
                                "smart_collection_id": 9999, "statuses": ["completed_100"]}}
        self.note = {"id": 150 if target else 50, "user_id": self.user["id"], "title": "Walkthrough",
            "content": "newer text" if target else "saved text", "tags": ["boss"], "is_public": False}
        self.save = {"id": 160 if target else 60, "user_id": self.user["id"], "rom_id": self.rid,
            "file_name": "Mario.srm", "emulator": "snes9x", "is_public": False, "is_favorite": True,
            "labels": ["complete"], "screenshot": None, "slot": "auto"}

    def request(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        path = urlsplit(url).path.removeprefix("/base")
        if path.startswith("/api"):
            path = path[4:]
        record = {"method": method, "path": path, **kwargs}
        if "files" in kwargs:
            record["files"] = {k: (f[0], f[1].read(), f[2]) for k, f in kwargs["files"].items()}
        self.calls.append(record)
        if method != "GET":
            return Reply({"id": 999}, status=201)
        if path == "/heartbeat":
            return Reply({"SYSTEM": {"VERSION": "5.4.0"}})
        if path == "/users/me":
            return Reply(self.user)
        if path == "/config":
            return Reply(self.cfg)
        if path == "/roms":
            hidden = kwargs["params"].get("statuses") == "hidden"
            return Reply({"items": [] if hidden else [self.rom], "total": 0 if hidden else 1})
        if path == "/roms/identifiers":
            return Reply([self.rid])
        if path == f"/roms/{self.rid}":
            return Reply(self.rom)
        if path == f"/roms/{self.rid}/notes":
            return Reply([self.note, {**self.note, "id": 555, "user_id": 555, "title": "Another user's public note"}])
        if path == "/platforms":
            return Reply([{"id": self.pid, "slug": "snes", "fs_slug": "snes", "custom_name": "Super Nintendo", "description": "My platform"}])
        if path == "/collections":
            return Reply([self.collection])
        if path == "/collections/smart":
            return Reply([self.smart])
        if path == "/saves":
            return Reply([self.save])
        if path == "/states":
            return Reply([])
        if path == "/memory-cards":
            return Reply([{"id": 70, "user_id": self.user["id"], "name": "Card", "emulator": "pcsx2", "platform_id": self.pid, "is_public": False}])
        if path == "/memory-cards/70/versions":
            return Reply([{"id": 72, "file_name": "latest.zip"}, {"id": 71, "file_name": "older.zip"}])
        if path.startswith("/memory-cards/versions/") and path.endswith("/content"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr("Mcd001.ps2", path.encode())
            return Reply(content=buf.getvalue())
        if path.endswith("/content") or path.startswith("/assets/romm/resources/"):
            return Reply(content=path.encode())
        raise AssertionError((method, path, kwargs))


def app(target=False):
    driver = RommApp("https://romm.example/base/", "fixture-secret-token")
    driver.session = Server(target)
    return driver


def exported(tmp_path):
    return Path(app().backup(tmp_path))


def rewrite(path, mutate):
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    doc = json.loads(files["manifest.json"])
    mutate(doc)
    files["manifest.json"] = json.dumps(doc).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def writes(driver):
    return [c for c in driver.session.calls if c["method"] != "GET"]


def test_export_restore_roundtrip_remaps_every_reference_and_keeps_binary_data(tmp_path):
    path = exported(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    target = app(True)
    with zipfile.ZipFile(path) as archive:
        doc = json.loads(archive.read("manifest.json"))
        assert "fixture-secret-token" not in json.dumps(doc)
        assert len(doc["roms"][0]["notes"]) == 1
        assert "image_path" not in doc["roms"][0]["metadata"]["igdb_metadata"]
        assert "smart_collection_id" not in doc["smart_collections"][0]["filter_criteria"]
        restore = RommRestore(target, archive)
        assert restore.plan() == {"roms": 1, "collections": 1, "assets": 1, "memory_cards": 1}
        assert writes(target) == []
        restore.apply()
    actions = writes(target)
    props = next(c for c in actions if c["path"] == "/roms/110/props")
    assert props["json"]["completion"] == 100
    assert any(c["path"] == "/roms/110/notes/150" and c["json"]["content"] == "saved text" for c in actions)
    assert any(c["path"] == "/saves/160" and c["files"]["saveFile"][1] == b"/saves/60/content" for c in actions)
    smart = next(c for c in actions if c["path"] == "/collections/smart/140")
    assert json.loads(smart["data"]["filter_criteria"]) == {"platform_ids": [120], "collection_id": 130, "statuses": ["completed_100"]}
    manual = next(c for c in actions if c["path"] == "/roms/110/manuals")
    assert manual["headers"] == {"x-upload-filename": "10.pdf"}
    assert manual["files"]["10.pdf"][1].endswith(b"manual/10.pdf")
    card_versions = [c["files"]["cardFile"][0] for c in actions if c["path"] == "/memory-cards/999/versions"]
    assert card_versions == ["older.zip", "latest.zip"]


@pytest.mark.parametrize("change, message", [
    (lambda d: d["owner"].update(username="someone-else"), "different username"),
    (lambda d: d["roms"][0]["identity"].update(sha1_hash="b" * 40), "uniquely match"),
    (lambda d: d["assets"][0]["blob"].update(sha256="bad"), "checksum"),
    (lambda d: d["assets"][0].update(file_name="../../secrets"), "unsafe asset"),
    (lambda d: d["collections"][0].update(rom_ids=[1234]), "outside the backup"),
    (lambda d: d["roms"][0]["props"].update(completion=101), "rating/completion"),
    (lambda d: d["roms"][0]["props"].update(user_id=55), "unsupported personal"),
    (lambda d: d["settings"]["converto"].update(cache_max_size_gb=-1), "conversion"),
    (lambda d: d["smart_collections"][0]["filter_criteria"].update(collection_id=999), "malformed"),
])
def test_preflight_rejects_bad_archive_before_any_write(tmp_path, change, message):
    path = exported(tmp_path)
    rewrite(path, change)
    target = app(True)
    with zipfile.ZipFile(path) as archive, pytest.raises(RommError, match=message):
        RommRestore(target, archive).plan()
    assert writes(target) == []


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_errors_and_redirects_do_not_expose_credentials(status):
    driver = app()
    driver.session.request = lambda *a, **k: Reply(status=status)
    with pytest.raises(RommError) as exc:
        driver.test_connection()
    assert "fixture-secret-token" not in str(exc.value)


def test_transport_errors_are_redacted():
    driver = app()
    def error(*args, **kwargs):
        raise requests.ConnectionError("private-token-in-url")
    driver.session.request = error
    with pytest.raises(RommError, match="connectivity") as exc:
        driver.test_connection()
    assert "private-token" not in str(exc.value)


def test_interrupted_asset_stream_discards_archive(tmp_path):
    driver = app()
    original = driver.session.request
    def get(*args, **kwargs):
        response = original(*args, **kwargs)
        if kwargs.get("stream"):
            def broken(size):
                yield b"partial"
                raise requests.ConnectionError("private-token-in-url")
            response.iter_content = broken
        return response
    driver.session.request = get
    with pytest.raises(RommError, match="interrupted"):
        driver.backup(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@romm/", "https://romm/?token=abc", "https://romm/#abc", "https://romm:bad/"])
def test_rejects_credential_bearing_or_invalid_url(url):
    with pytest.raises(RommError):
        RommApp(url, "token")


@pytest.mark.parametrize("path", ["https://evil/cover.jpg", "//evil/cover.jpg", "/assets/romm/resources/../token", "/assets/romm/resources/%252e%252e/token"])
def test_resource_urls_cannot_escape_origin_or_resource_directory(path):
    driver = app()
    with zipfile.ZipFile(io.BytesIO(), "w") as archive, pytest.raises(RommError, match="unsafe"):
        driver._resource(archive, path, "cover")
    assert driver.session.calls == []


def test_native_settings_and_factory_registration():
    from backup import build_app, humanize_error
    from config_store import APP_META, DEFAULTS
    assert "romm" in DEFAULTS["apps"]
    assert any(m["id"] == "romm" and not m["restore_supported"] for m in APP_META)
    assert isinstance(build_app("romm", {"url": "http://romm", "api_key": "token"}), RommApp)
    assert humanize_error(RommError("romm: useful error")) == "romm: useful error"


def test_metadata_strips_nested_source_cache_paths_but_preserves_content():
    assert portable_metadata({"image_path": "old", "physical_discs": [{"path": "old", "url": "https://provider/disc"}]}) == {
        "physical_discs": [{"url": "https://provider/disc"}]}


def test_late_preflight_failure_cannot_be_applied(tmp_path):
    path = exported(tmp_path)
    target = app(True)
    target.session.collection["is_favorite"] = False
    with zipfile.ZipFile(path) as archive:
        restore = RommRestore(target, archive)
        with pytest.raises(RommError, match="favorite type"):
            restore.plan()
        with pytest.raises(RommError, match="preflight"):
            restore.apply()
    assert writes(target) == []


def test_foreign_account_assets_abort_backup(tmp_path):
    driver = app()
    driver.session.save["user_id"] = 987
    with pytest.raises(RommError, match="another user's"):
        driver.backup(tmp_path)
    assert not list(tmp_path.iterdir())


def test_connection_checks_read_scope_beyond_user_profile():
    driver = app()
    original = driver.session.request
    def deny_assets(method, url, **kwargs):
        return Reply(status=403) if url.endswith("/saves") else original(method, url, **kwargs)
    driver.session.request = deny_assets
    with pytest.raises(RommError, match="403"):
        driver.test_connection()


def test_read_only_target_config_fails_before_writes(tmp_path):
    path = exported(tmp_path)
    target = app(True)
    target.session.cfg["CONFIG_FILE_WRITABLE"] = False
    with zipfile.ZipFile(path) as archive, pytest.raises(RommError, match="writable"):
        RommRestore(target, archive).plan()
    assert writes(target) == []


def test_same_platform_slug_with_distinct_folders_is_mapped_without_ambiguity(tmp_path):
    path = exported(tmp_path)
    rewrite(path, lambda d: d["platforms"].append({"id": 21, "slug": "snes", "fs_slug": "snes-hacks"}))
    target = app(True)
    original = target.session.request
    def platforms(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if url.endswith("/platforms"):
            response.data.append({"id": 121, "slug": "snes", "fs_slug": "snes-hacks"})
        return response
    target.session.request = platforms
    with zipfile.ZipFile(path) as archive:
        restore = RommRestore(target, archive)
        restore.plan()
        assert restore.platform_map == {20: 120, 21: 121}


def test_pagination_reads_every_visible_and_hidden_rom():
    driver = app()
    calls = []
    def pages(method, path, *, params):
        calls.append(params)
        if params.get("statuses") == "hidden":
            return {"total": 1, "items": [{"id": 102}]}
        return {"total": 101, "items": [{"id": i} for i in range(params["offset"] + 1, min(params["offset"] + 101, 102))]}
    driver.json = pages
    assert len(driver.roms()) == 102
    assert [c["offset"] for c in calls] == [0, 100, 0]
    assert calls[-1]["statuses"] == "hidden"
