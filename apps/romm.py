"""RomM 5.4 API backup of one account and editable library settings.

See docs/romm.md for the API contract, restore procedure and exclusions.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit
import zipfile

import requests

log = logging.getLogger("backuparr.romm")
FORMAT = "backuparr-romm-api-v1"
SUPPORTED_VERSION = "5.4.0"
PROPS = {"is_main_sibling", "backlogged", "now_playing", "hidden", "rating",
         "difficulty", "completion", "status"}
PROVIDERS = ("igdb", "moby", "ss", "launchbox", "hasheous", "flashpoint", "hltb",
             "demozoo", "pouet", "csdb", "steam")
METADATA = {"name", "name_sort_key", "summary", "manual_metadata"}
METADATA.update(f"{p}_{suffix}" for p in PROVIDERS for suffix in ("id", "metadata"))
METADATA.update({"sgdb_id", "ra_id", "tgdb_id", "libretro_id"})
EXCLUSIONS = ("EXCLUDED_PLATFORMS", "EXCLUDED_SINGLE_EXT", "EXCLUDED_SINGLE_FILES",
              "EXCLUDED_MULTI_FILES", "EXCLUDED_MULTI_PARTS_EXT", "EXCLUDED_MULTI_PARTS_FILES")
LIMITATIONS = [
    "Only the token owner's personal data; other accounts are not included, even with an admin token.",
    "No ROM/BIOS binaries, database, passwords, tokens, environment secrets or full config.yml.",
    "No exact last-played timestamps, play-session history, achievements, device/sync history or browser preferences.",
    "No music, patches, permission groups or generated virtual collections.",
    "Save/state IDs and timestamps are regenerated; uploads change last-played to restore time.",
    "New slotted saves are uploaded as ordinary saves to avoid server slot retention; existing same-name saves retain their target slot.",
    "Memory cards are recreated, with new IDs/timestamps; repeated restores create additional cards.",
    "An absent avatar or empty RetroAchievements username does not clear existing target profile data; the API has no clear operation.",
]


class RommError(RuntimeError):
    pass


def fail(message):
    raise RommError("romm: " + message)


def ident(value):
    if type(value) is not int or value < 1:
        fail("invalid object ID in API response or backup")
    return value


def rows(value):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        fail("invalid list in API response or backup")
    return value


def pick(value, keys):
    return {k: value[k] for k in keys if k in value}


def portable_metadata(value):
    """Provider cache paths embed source DB IDs; RomM regenerates those files."""
    if isinstance(value, list):
        return [portable_metadata(v) for v in value]
    if isinstance(value, dict):
        return {k: portable_metadata(v) for k, v in value.items()
                if k != "path" and not k.endswith("_path")}
    return value


def filename(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(c in value for c in "/\\\x00\r\n")):
        fail("unsafe asset filename")
    return value


def settings(config):
    """Only expose settings with a corresponding public write endpoint."""
    scan = {
        "metadata_priority": config["SCAN_METADATA_PRIORITY"],
        "artwork_priority": config["SCAN_ARTWORK_PRIORITY"],
        "region_priority": config["SCAN_REGION_PRIORITY"],
        "language_priority": config["SCAN_LANGUAGE_PRIORITY"],
        "media": config["SCAN_MEDIA"],
        "gamelist_export": config["GAMELIST_AUTO_EXPORT_ON_SCAN"],
        "gamelist_thumbnail": config["GAMELIST_MEDIA_THUMBNAIL"],
        "gamelist_image": config["GAMELIST_MEDIA_IMAGE"],
        "pegasus_export": config["PEGASUS_AUTO_EXPORT_ON_SCAN"],
    }
    for key in ("cover", "screenshot", "manual"):
        scan[key + "_priority"] = config["SCAN_ARTWORK_PRIORITY_OVERRIDES"].get(key)
    return {
        "scan": scan,
        "converto": pick(config["CONVERTO"], {"download_conversion_enabled", "cache_max_size_gb", "platform_formats"}),
        "platforms": config["PLATFORMS_BINDING"],
        "versions": config["PLATFORMS_VERSIONS"],
        "exclusions": pick(config, EXCLUSIONS),
    }


class RommApp:
    def __init__(self, url, api_key, timeout=60):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment)
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            fail("use an http(s) server URL without credentials, query or fragment")
        if not api_key or "\n" in api_key or "\r" in api_key:
            fail("a RomM client token is required")
        self.url, self.timeout = url.rstrip("/"), timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    def request(self, method, path, *, resource=False, **kwargs):
        try:
            response = self.session.request(method, self.url + ("" if resource else "/api") + path,
                                            timeout=self.timeout, allow_redirects=False, **kwargs)
        except requests.RequestException:
            fail("API request failed; check server connectivity and TLS")
        if 300 <= response.status_code < 400:
            response.close()
            fail("redirect refused; use the final RomM server URL")
        if response.status_code not in (200, 201, 204):
            status = response.status_code
            response.close()
            fail(f"API request failed (HTTP {status}); check token permissions and server logs")
        return response

    def json(self, method, path, **kwargs):
        with self.request(method, path, **kwargs) as response:
            try:
                return response.json()
            except ValueError:
                fail("server did not return valid API JSON")

    def identity(self):
        heartbeat = self.json("GET", "/heartbeat")
        if (not isinstance(heartbeat, dict) or not isinstance(heartbeat.get("SYSTEM"), dict)
                or heartbeat["SYSTEM"].get("VERSION") != SUPPORTED_VERSION):
            fail(f"this backup format currently supports RomM {SUPPORTED_VERSION}; refusing an unverified API version")
        user = self.json("GET", "/users/me")
        if not isinstance(user, dict) or not user.get("username"):
            fail("token did not identify a user")
        ident(user.get("id"))
        if user.get("role") != "admin":
            fail("an administrator client token is required for library metadata/settings")
        return user

    def test_connection(self):
        self.identity()
        for path in ("/roms/identifiers", "/collections", "/collections/smart", "/saves", "/states", "/platforms", "/memory-cards"):
            self.json("GET", path)
        return f"RomM {SUPPORTED_VERSION} reachable (token owner's data only; see backup scope)"

    def roms(self):
        result = {}
        # Ordinary listings exclude user-hidden games. Query that disjoint set too.
        for hidden in (False, True):
            offset, total = 0, None
            while total is None or offset < total:
                params = {"offset": offset, "limit": 100, "order_by": "id", "order_dir": "asc",
                          "group_by_meta_id": "false", "with_char_index": "false",
                          "with_filter_values": "false", "with_rom_id_index": "false"}
                if hidden:
                    params["statuses"] = "hidden"
                page = self.json("GET", "/roms", params=params)
                if not isinstance(page, dict):
                    fail("invalid ROM listing")
                count = page.get("total")
                if type(count) is not int or count < 0 or (total is not None and total != count):
                    fail("ROM listing changed during backup; retry with scans paused")
                total = count
                batch = rows(page.get("items"))
                if not batch and offset < total:
                    fail("incomplete ROM listing")
                for item in batch:
                    item_id = ident(item.get("id"))
                    if item_id in result:
                        fail("duplicate ROM in listing; retry with scans paused")
                    result[item_id] = item
                offset += len(batch)
                if offset > total:
                    fail("inconsistent ROM listing")
        return list(result.values())

    def _blob(self, archive, path, member, *, resource=False):
        digest, size = hashlib.sha256(), 0
        try:
            with self.request("GET", path, stream=True, resource=resource) as response, archive.open(member, "w") as output:
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    fail("asset download returned HTML; check reverse proxy authentication")
                for chunk in response.iter_content(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except requests.RequestException:
            fail("asset download interrupted; backup was discarded")
        return {"member": member, "sha256": digest.hexdigest(), "size": size}

    def _resource(self, archive, path, member, *, manual=False):
        # RomM returns same-origin resource paths, never trust an absolute URL.
        if manual:
            path = "/assets/romm/resources/" + path.lstrip("/")
        parsed = urlsplit(path)
        decoded = unquote(parsed.path)
        if (parsed.scheme or parsed.netloc or not decoded.startswith("/assets/romm/resources/")
                or any(part in (".", "..") for part in decoded.split("/")) or "\\" in decoded or "%" in decoded):
            fail("unsafe RomM resource path")
        name = filename(decoded.rsplit("/", 1)[-1])
        return {"file_name": name, "blob": self._blob(archive, quote(decoded, safe="/"), member, resource=True)}

    def backup(self, dest_dir):
        user = self.identity()
        doc = {"format": FORMAT, "server_version": SUPPORTED_VERSION,
               "owner": pick(user, {"username", "id"}), "limitations": LIMITATIONS,
               "profile": {k: user.get(k) or "" for k in ("email", "ra_username")},
               "settings": settings(self.json("GET", "/config")),
               "roms": [], "collections": [], "smart_collections": [], "assets": [], "memory_cards": [],
               "platforms": [pick(p, {"id", "slug", "fs_slug", "custom_name", "description"}) for p in rows(self.json("GET", "/platforms"))]}
        catalog = self.roms()
        log.info("romm: found %d ROMs; exporting metadata and personal notes", len(catalog))
        os.makedirs(dest_dir, exist_ok=True)
        output = Path(dest_dir) / "romm-api-export.zip"
        try:
            with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    if user.get("avatar_path"):
                        doc["profile"]["avatar"] = {"file_name": filename(user["avatar_path"].rsplit("/", 1)[-1]),
                            "blob": self._blob(archive, f"/users/{ident(user['id'])}/avatar", f"files/users/{user['id']}-avatar")}
                    screenshots = {}
                    for item in catalog:
                        item_id = ident(item["id"])
                        detail = self.json("GET", f"/roms/{item_id}")
                        notes = rows(self.json("GET", f"/roms/{item_id}/notes"))
                        record = {
                            "id": item_id,
                            "identity": pick(detail, {"platform_slug", "fs_name", "fs_path", "sha1_hash", "md5_hash"}),
                            "metadata": portable_metadata(pick(detail, METADATA)),
                            "props": pick(detail["rom_user"], PROPS),
                            "notes": [pick(n, {"title", "content", "tags", "is_public"})
                                      for n in notes if n["user_id"] == user["id"]],
                        }
                        for key, field, manual in (("cover", "path_cover_large", False), ("manual", "path_manual", True)):
                            if detail.get(field):
                                record[key] = self._resource(archive, detail[field], f"files/roms/{item_id}-{key}", manual=manual)
                        doc["roms"].append(record)
                        for shot in detail.get("user_screenshots", []):
                            if shot.get("is_gallery") and shot["user_id"] == user["id"]:
                                screenshots[ident(shot["id"])] = shot
                    collections = rows(self.json("GET", "/collections"))
                    doc["collection_references"] = [pick(c, {"id", "name", "owner_username"}) for c in collections]
                    for collection in collections:
                        if collection["user_id"] == user["id"]:
                            record = pick(collection, {"id", "name", "description", "is_public", "is_favorite", "rom_ids"})
                            if collection.get("path_cover_large"):
                                record["cover"] = self._resource(archive, collection["path_cover_large"], f"files/collections/{ident(collection['id'])}-cover")
                            doc["collections"].append(record)
                    doc["smart_collections"] = [pick(c, {"id", "name", "description", "is_public", "filter_criteria"})
                        for c in rows(self.json("GET", "/collections/smart")) if c["user_id"] == user["id"]]
                    for collection in doc["smart_collections"]:
                        # Upstream ignores this UI route hint, not a dependency.
                        collection["filter_criteria"].pop("smart_collection_id", None)
                    for kind in ("saves", "states", "screenshots"):
                        assets = list(screenshots.values()) if kind == "screenshots" else rows(self.json("GET", "/" + kind))
                        for asset in assets:
                            if asset["user_id"] != user["id"]:
                                fail("asset listing contained another user's data")
                            asset_id = ident(asset["id"])
                            data = pick(asset, {"rom_id", "emulator", "is_public", "is_favorite", "labels"})
                            data.update({"kind": kind, "file_name": filename(asset["file_name"])})
                            data["blob"] = self._blob(archive, f"/{kind}/{asset_id}/content", f"files/{kind}/{asset_id}")
                            shot = asset.get("screenshot")
                            if shot:
                                sid = ident(shot["id"])
                                data["screenshot"] = {"file_name": filename(shot["file_name"]),
                                    "blob": self._blob(archive, f"/screenshots/{sid}/content", f"files/{kind}/{asset_id}-screenshot")}
                            doc["assets"].append(data)
                        log.info("romm: completed %s (%d files)", kind, len(assets))
                    for card in rows(self.json("GET", "/memory-cards")):
                        if card["user_id"] != user["id"]:
                            fail("memory card listing contained another user's data")
                        cid = ident(card["id"])
                        record = pick(card, {"id", "name", "emulator", "platform_id", "is_public"})
                        record["versions"] = []
                        for version in reversed(rows(self.json("GET", f"/memory-cards/{cid}/versions"))):
                            vid = ident(version["id"])
                            record["versions"].append({"file_name": filename(version["file_name"]),
                                "blob": self._blob(archive, f"/memory-cards/versions/{vid}/content", f"files/memory-cards/{vid}")})
                        doc["memory_cards"].append(record)
                    log.info("romm: completed memory cards (%d)", len(doc["memory_cards"]))
                    archive.writestr("manifest.json", json.dumps(doc, ensure_ascii=False))
            # Never report success for an export that the importer cannot read.
            from apps.romm_restore import RommRestore
            with zipfile.ZipFile(output) as archive:
                RommRestore(self, archive).validate()
            log.info("romm: backup complete; one account, %d ROMs, %d assets", len(catalog), len(doc["assets"]))
            return str(output)
        except Exception:
            output.unlink(missing_ok=True)
            raise
