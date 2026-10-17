"""Preflight/restore RomM exports: python -m apps.romm_restore --help."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import ntpath
from pathlib import Path
import re
import sys
from urllib.parse import quote
import zipfile

from apps.http_upload import multipart_body
from apps.romm import (EXCLUSIONS, FORMAT, METADATA, PROPS, SUPPORTED_VERSION,
                       RommApp, RommError, fail, filename, ident, pick, rows, settings)


def match_roms(old, current):
    mapping, used = {}, set()
    for item in old:
        identity = item["identity"]
        candidates = [r for r in current if r.get("platform_slug") == identity["platform_slug"]]
        # Strong content identity first, with the relative path breaking ties
        # between duplicate copies. Never use a display title or source DB ID.
        for key in ("sha1_hash", "md5_hash"):
            if identity.get(key):
                candidates = [r for r in candidates if r.get(key) == identity[key]]
                break
        else:
            candidates = [r for r in candidates if (r.get("fs_path"), r.get("fs_name")) ==
                          (identity["fs_path"], identity["fs_name"])]
        if len(candidates) > 1:
            candidates = [r for r in candidates if (r.get("fs_path"), r.get("fs_name")) ==
                          (identity["fs_path"], identity["fs_name"])]
        if len(candidates) != 1 or candidates[0]["id"] in used:
            fail("cannot uniquely match every ROM; restore/rescan the original ROM library first")
        new_id = ident(candidates[0]["id"])
        mapping[item["id"]] = new_id
        used.add(new_id)
    return mapping


class RommRestore:
    def __init__(self, app, archive):
        self.app, self.archive = app, archive
        self.doc, self.mapping = None, None
        self.planned = False
        self.collections, self.existing_notes = {}, {}

    def _check_blob(self, blob, names, members):
        member = blob["member"]
        if not re.fullmatch(r"files/(saves|states|screenshots|roms|collections|memory-cards|users)/[1-9][0-9]*(?:-(?:screenshot|cover|manual|avatar))?", member):
            fail("invalid asset archive path")
        if member in members or member not in names:
            fail("missing or duplicate asset data")
        members.add(member)
        digest = hashlib.sha256()
        with self.archive.open(member) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != blob["sha256"] or self.archive.getinfo(member).file_size != blob["size"]:
            fail("asset checksum/size mismatch")

    def _check_card(self, blob):
        # RomM's upload accepts ZIP card sets only, capped at 256 MiB, and
        # rejects escaping paths and symlinks. Catch those before any writes.
        budget = 256 * 1024 * 1024
        if blob["size"] > budget:
            fail("memory card exceeds RomM's upload limit")
        try:
            with self.archive.open(blob["member"]) as source, zipfile.ZipFile(source) as card:
                for entry in card.infolist():
                    if (entry.filename.startswith("/") or ntpath.isabs(entry.filename)
                            or ".." in re.split(r"[\\/]", entry.filename)
                            or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                        fail("unsafe memory card archive")
                    with card.open(entry) as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            budget -= len(chunk)
                            if budget < 0:
                                fail("expanded memory card exceeds RomM's limit")
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
            fail("memory card is not a supported ZIP archive")

    def validate(self):
        """Validate all references and file checksums before any server write."""
        names = self.archive.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            fail("invalid archive members")
        if self.archive.getinfo("manifest.json").file_size > 50 * 1024 * 1024:
            fail("manifest is too large")
        try:
            doc = json.loads(self.archive.read("manifest.json"))
            if doc["format"] != FORMAT or doc["server_version"] != SUPPORTED_VERSION:
                fail("unsupported backup format or RomM version")
            if not isinstance(doc["owner"]["username"], str):
                fail("invalid backup owner")
            ids = set()
            for rom in rows(doc["roms"]):
                rid = ident(rom["id"])
                if rid in ids:
                    fail("duplicate ROM ID in backup")
                ids.add(rid)
                for key in ("platform_slug", "fs_path", "fs_name"):
                    if not isinstance(rom["identity"][key], str):
                        fail("invalid ROM identity")
                if not isinstance(rom["metadata"], dict) or set(rom["metadata"]) - METADATA:
                    fail("unsupported metadata fields")
                if not isinstance(rom["props"], dict) or set(rom["props"]) - PROPS:
                    fail("unsupported personal properties")
                for key, value in rom["props"].items():
                    if key in ("rating", "difficulty", "completion"):
                        if type(value) is not int or not 0 <= value <= (100 if key == "completion" else 10):
                            fail("invalid personal rating/completion")
                    elif key == "status":
                        if value not in (None, "incomplete", "finished", "completed_100", "retired", "never_playing"):
                            fail("invalid personal status")
                    elif type(value) is not bool:
                        fail("invalid personal flag")
                for key, value in rom["metadata"].items():
                    if key.endswith("_metadata"):
                        if value is not None and not isinstance(value, dict):
                            fail("invalid metadata object")
                    elif key in ("name", "name_sort_key", "summary"):
                        if value is not None and not isinstance(value, str):
                            fail("invalid metadata text")
                    elif value is not None and not isinstance(value, (str, int)):
                        fail("invalid metadata provider ID")
                titles = set()
                for note in rows(rom["notes"]):
                    if set(note) != {"title", "content", "tags", "is_public"}:
                        fail("invalid note fields")
                    if not isinstance(note["title"], str) or not isinstance(note["content"], str):
                        fail("invalid note text")
                    if note["title"] in titles or type(note["is_public"]) is not bool or (note["tags"] is not None and
                            (not isinstance(note["tags"], list) or any(not isinstance(t, str) for t in note["tags"]))):
                        fail("invalid or duplicate note")
                    titles.add(note["title"])
            collection_names = set()
            for collection in rows(doc["collections"]):
                name = collection["name"]
                if not isinstance(name, str) or not name or name in collection_names:
                    fail("invalid or duplicate collection name")
                collection_names.add(name)
                if not isinstance(collection["description"], str) or any(type(collection[k]) is not bool for k in ("is_public", "is_favorite")):
                    fail("invalid collection fields")
                if not isinstance(collection["rom_ids"], list) or any(ident(i) not in ids for i in collection["rom_ids"]):
                    fail("collection references a ROM outside the backup")
                if len(collection["rom_ids"]) != len(set(collection["rom_ids"])):
                    fail("duplicate collection member")
            members, asset_keys = {"manifest.json"}, set()
            profile = doc["profile"]
            if not isinstance(profile, dict) or set(profile) - {"email", "ra_username", "avatar"} or any(not isinstance(profile[k], str) for k in ("email", "ra_username")):
                fail("invalid profile fields")
            if profile.get("avatar"):
                filename(profile["avatar"]["file_name"])
                self._check_blob(profile["avatar"]["blob"], names, members)
            for card in rows(doc["memory_cards"]):
                ident(card["id"])
                if not isinstance(card["name"], str) or not card["name"].strip() or type(card["is_public"]) is not bool:
                    fail("invalid memory card")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", card["emulator"]):
                    fail("invalid memory card emulator")
                for version in rows(card["versions"]):
                    filename(version["file_name"])
                    self._check_blob(version["blob"], names, members)
                    self._check_card(version["blob"])
            for record in doc["roms"] + doc["collections"]:
                for field in ("cover", "manual"):
                    if record.get(field):
                        filename(record[field]["file_name"])
                        self._check_blob(record[field]["blob"], names, members)
            for asset in rows(doc["assets"]):
                if asset["kind"] not in ("saves", "states", "screenshots") or ident(asset["rom_id"]) not in ids:
                    fail("invalid asset reference")
                filename(asset["file_name"])
                for key in ("is_public", "is_favorite"):
                    if key in asset and type(asset[key]) is not bool:
                        fail("invalid asset flag")
                if "labels" in asset and (not isinstance(asset["labels"], list) or any(not isinstance(x, str) for x in asset["labels"])):
                    fail("invalid asset labels")
                emulator = asset.get("emulator")
                if emulator is not None:
                    filename(emulator)
                key = (asset["kind"], asset["rom_id"], emulator, asset["file_name"])
                if key in asset_keys:
                    fail("colliding asset filenames cannot be safely restored")
                asset_keys.add(key)
                blobs = [asset["blob"]]
                if asset.get("screenshot"):
                    filename(asset["screenshot"]["file_name"])
                    blobs.append(asset["screenshot"]["blob"])
                for blob in blobs:
                    self._check_blob(blob, names, members)
            if members != set(names):
                fail("unexpected archive members")
            for key in ("platforms", "collection_references", "smart_collections"):
                seen = set()
                for item in rows(doc[key]):
                    item_id = ident(item["id"])
                    if item_id in seen:
                        fail("duplicate reference ID")
                    seen.add(item_id)
            for platform in doc["platforms"]:
                for key in ("slug", "fs_slug"):
                    if not isinstance(platform[key], str):
                        fail("invalid platform identity")
                if any(platform.get(k) is not None and not isinstance(platform[k], str) for k in ("custom_name", "description")):
                    fail("invalid platform metadata")
            if len({c["name"] for c in doc["smart_collections"]}) != len(doc["smart_collections"]):
                fail("duplicate smart collection name")
            for collection in doc["smart_collections"]:
                if (not isinstance(collection["filter_criteria"], dict) or not isinstance(collection["name"], str)
                        or not isinstance(collection["description"], str) or type(collection["is_public"]) is not bool):
                    fail("invalid smart collection filters")
                self._translate_filters(collection["filter_criteria"],
                    {x["id"]: x["id"] for x in doc["platforms"]},
                    {x["id"]: x["id"] for x in doc["collection_references"]},
                    {x["id"]: x["id"] for x in doc["smart_collections"]})
            config = doc["settings"]
            if set(config) != {"scan", "converto", "platforms", "versions", "exclusions"}:
                fail("invalid settings sections")
            if set(config["exclusions"]) != set(EXCLUSIONS):
                fail("incomplete exclusion settings")
            for section in ("platforms", "versions"):
                if not isinstance(config[section], dict):
                    fail("invalid platform settings")
                for key, value in config[section].items():
                    filename(key)
                    if not isinstance(value, str):
                        fail("invalid platform binding")
            for values in config["exclusions"].values():
                if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                    fail("invalid exclusion values")
            for key, value in config["scan"].items():
                if key.endswith("_priority") or key == "media":
                    if value is None and key in ("cover_priority", "screenshot_priority", "manual_priority"):
                        continue
                    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                        fail("invalid scan priorities")
                elif key in ("gamelist_export", "pegasus_export"):
                    if type(value) is not bool:
                        fail("invalid scan flag")
                elif key in ("gamelist_thumbnail", "gamelist_image"):
                    if not isinstance(value, str):
                        fail("invalid scan artwork type")
                else:
                    fail("unknown scan setting")
            converto = config["converto"]
            if (type(converto["download_conversion_enabled"]) is not bool or
                    type(converto["cache_max_size_gb"]) is not int or converto["cache_max_size_gb"] < 0 or
                    not isinstance(converto["platform_formats"], dict) or
                    any(not isinstance(k, str) or not isinstance(v, str) for k, v in converto["platform_formats"].items())):
                fail("invalid conversion settings")
        except (KeyError, TypeError, ValueError, zipfile.BadZipFile):
            fail("malformed or incomplete backup")
        self.doc = doc
        return doc

    @staticmethod
    def _translate_filters(filters, platforms, collections, smart):
        result = dict(filters)
        result.pop("smart_collection_id", None)
        for key, value in filters.items():
            if value is None:
                continue
            if key in ("platform_ids", "platform_id"):
                result[key] = [platforms[ident(i)] for i in value] if key.endswith("ids") else platforms[ident(value)]
            elif key == "collection_id":
                result[key] = collections[ident(value)]
            elif key == "smart_collection_id":
                continue
            elif key.endswith(("_id", "_ids")) and key not in ("virtual_collection_id", "group_by_meta_id"):
                fail("unsupported ID reference in smart collection")
        return result

    def plan(self):
        self.planned = False
        self.collections, self.existing_notes = {}, {}
        doc = self.validate()
        user = self.app.identity()
        self.user_id = user["id"]
        if user["username"] != doc["owner"]["username"]:
            fail("target token belongs to a different username; use the original account")
        config = self.app.json("GET", "/config")
        if not config.get("CONFIG_FILE_WRITABLE"):
            fail("target config.yml must be writable by RomM to restore settings")
        self.current_settings = settings(config)
        if set(doc["settings"]["scan"]) != set(self.current_settings["scan"]) or set(doc["settings"]["converto"]) != set(self.current_settings["converto"]):
            fail("settings schema does not match the target")
        for key in EXCLUSIONS:
            if any("/" in value for value in set(self.current_settings["exclusions"][key]) - set(doc["settings"]["exclusions"][key])):
                fail("remove target-only exclusions containing '/' in RomM config before restoring; its delete API cannot address them")
        self.mapping = match_roms(doc["roms"], self.app.roms())
        platforms = rows(self.app.json("GET", "/platforms"))
        self.platform_map = {}
        for platform in doc["platforms"]:
            matches = [p for p in platforms if (p["slug"], p["fs_slug"]) == (platform["slug"], platform["fs_slug"])]
            if len(matches) != 1:
                fail("cannot uniquely match every platform; recreate platforms first")
            self.platform_map[platform["id"]] = ident(matches[0]["id"])
        if any(card.get("platform_id") is not None and card["platform_id"] not in self.platform_map for card in doc["memory_cards"]):
            fail("memory card references a missing platform")
        all_collections = rows(self.app.json("GET", "/collections"))
        self.collection_map = {}
        for reference in doc["collection_references"]:
            matches = [c for c in all_collections if (c["name"], c["owner_username"]) == (reference["name"], reference["owner_username"])]
            if len(matches) > 1:
                fail("ambiguous target collection reference")
            if matches:
                self.collection_map[reference["id"]] = ident(matches[0]["id"])
        for collection in all_collections:
            if collection["user_id"] == user["id"]:
                if collection["name"] in self.collections:
                    fail("ambiguous target collection name")
                self.collections[collection["name"]] = collection
        for collection in doc["collections"]:
            current = self.collections.get(collection["name"])
            if current and current["is_favorite"] != collection["is_favorite"]:
                fail("target collection favorite type differs; rename it before restoring")
        self.smart = {c["name"]: c for c in rows(self.app.json("GET", "/collections/smart")) if c["user_id"] == user["id"]}
        # Unknown references must fail now, before creating/updating anything.
        try:
            for collection in doc["smart_collections"]:
                self._translate_filters(collection["filter_criteria"], self.platform_map,
                    {**self.collection_map, **{c["id"]: c["id"] for c in doc["collections"]}},
                    {c["id"]: c["id"] for c in doc["smart_collections"]})
        except KeyError:
            fail("smart collection references a missing public collection/platform")
        for rom in doc["roms"]:
            new_id = self.mapping[rom["id"]]
            self.existing_notes[rom["id"]] = [pick(n, {"id", "title", "content", "tags", "is_public"})
                for n in rows(self.app.json("GET", f"/roms/{new_id}/notes")) if n["user_id"] == user["id"]]
        # Fetch all target assets now, so missing read permissions fail before writes.
        self.existing_assets = {}
        for kind in ("saves", "states"):
            for asset in rows(self.app.json("GET", "/" + kind)):
                if asset["user_id"] == user["id"]:
                    key = (kind, asset["rom_id"], asset.get("emulator"), asset["file_name"])
                    if key in self.existing_assets:
                        fail("ambiguous target asset filename")
                    self.existing_assets[key] = ident(asset["id"])
        self.planned = True
        return {"roms": len(doc["roms"]), "collections": len(doc["collections"]), "assets": len(doc["assets"]), "memory_cards": len(doc["memory_cards"])}

    def write(self, method, path, **kwargs):
        if "files" in kwargs:
            upload = self._upload_data(kwargs.pop("files"), kwargs.pop("data", None))
            kwargs["headers"] = {**kwargs.get("headers", {}), **upload.pop("headers")}
            kwargs.update(upload)
        with self.app.request(method, path, **kwargs):
            pass

    def _upload_data(self, files, data=None):
        payload = multipart_body({
            field: (filename, stream, self.archive.getinfo(stream.name).file_size, mime)
            for field, (filename, stream, mime) in files.items()
        }, data)
        return {"data": payload, "headers": {"Content-Type": payload.content_type}}

    def upload(self, path, field, resource, **kwargs):
        with self.archive.open(resource["blob"]["member"]) as stream:
            self.write("POST" if field == resource["file_name"] else "PUT", path,
                files={field: (resource["file_name"], stream, "application/octet-stream")}, **kwargs)

    def apply(self):
        if not self.planned:
            fail("restore preflight must succeed before applying")
        doc = self.doc
        for card in doc["memory_cards"]:
            payload = pick(card, {"name", "emulator", "is_public"})
            if card.get("platform_id") is not None:
                payload["platform_id"] = self.platform_map[card["platform_id"]]
            new_card = self.app.json("POST", "/memory-cards", json=payload)
            cid = ident(new_card["id"])
            for version in card["versions"]:
                with self.archive.open(version["blob"]["member"]) as stream:
                    self.write("POST", f"/memory-cards/{cid}/versions", files={"cardFile": (version["file_name"], stream, "application/zip")})
        for asset in doc["assets"]:
            kind = asset["kind"]
            rid = self.mapping[asset["rom_id"]]
            key = (kind, rid, asset.get("emulator"), asset["file_name"])
            existing = self.existing_assets.get(key)
            field = {"saves": "saveFile", "states": "stateFile", "screenshots": "screenshotFile"}[kind]
            with ExitStack() as stack:
                files = {field: (asset["file_name"], stack.enter_context(self.archive.open(asset["blob"]["member"])), "application/octet-stream")}
                if asset.get("screenshot"):
                    shot = asset["screenshot"]
                    files["screenshotFile"] = (shot["file_name"], stack.enter_context(self.archive.open(shot["blob"]["member"])), "application/octet-stream")
                params = {"rom_id": rid}
                if asset.get("emulator"):
                    params["emulator"] = asset["emulator"]
                if kind == "saves":
                    params.update({"overwrite": "true", "autocleanup": "false"})
                response = self.app.json("PUT" if existing else "POST",
                    f"/{kind}/{existing}" if existing else f"/{kind}",
                    params={} if existing else params, **self._upload_data(files))
            aid = ident(response["id"])
            if kind == "screenshots":
                self.write("PUT", f"/screenshots/{aid}", json={"is_public": asset.get("is_public", False)})
            else:
                for suffix, name, default in (("visibility", "is_public", False), ("favorite", "is_favorite", False), ("labels", "labels", [])):
                    self.write("PUT", f"/{kind}/{aid}/{suffix}", json={name: asset.get(name, default)})
        for rom in doc["roms"]:
            rid = self.mapping[rom["id"]]
            form = {}
            for key, value in rom["metadata"].items():
                if key.endswith("_metadata"):
                    form["raw_" + key] = json.dumps(value or {})
                else:
                    form[key] = "" if value is None else str(value)
            # Provider-ID changes can refresh metadata. Reapply raw metadata
            # after IDs settle so restored edits win over provider responses.
            self.write("PUT", f"/roms/{rid}", data=form)
            self.write("PUT", f"/roms/{rid}", data=form)
            if rom.get("cover"):
                self.upload(f"/roms/{rid}", "artwork", rom["cover"])
            if rom.get("manual"):
                resource = rom["manual"]
                self.upload(f"/roms/{rid}/manuals", resource["file_name"], resource,
                            headers={"x-upload-filename": resource["file_name"]})
            self.write("PUT", f"/roms/{rid}/props", json=rom["props"])
            existing = {n["title"]: ident(n["id"]) for n in self.existing_notes[rom["id"]]}
            for note in rom["notes"]:
                nid = existing.get(note["title"])
                self.write("PUT" if nid else "POST", f"/roms/{rid}/notes/{nid}" if nid else f"/roms/{rid}/notes", json=note)
        for platform in doc["platforms"]:
            self.write("PUT", f"/platforms/{self.platform_map[platform['id']]}",
                       json={k: platform.get(k) or "" for k in ("custom_name", "description")})
        for collection in doc["collections"]:
            current = self.collections.get(collection["name"])
            if not current:
                current = self.app.json("POST", "/collections", data=pick(collection, {"name", "description"}),
                    params={k: str(collection[k]).lower() for k in ("is_public", "is_favorite")})
            cid = ident(current["id"])
            self.collection_map[collection["id"]] = cid
            self.write("PUT", f"/collections/{cid}", params={"is_public": str(collection["is_public"]).lower()},
                       data={**pick(collection, {"name", "description"}), "rom_ids": json.dumps([self.mapping[i] for i in collection["rom_ids"]])})
            if collection.get("cover"):
                self.upload(f"/collections/{cid}", "artwork", collection["cover"],
                            data={"rom_ids": json.dumps([self.mapping[i] for i in collection["rom_ids"]])})
        smart_map = {}
        for collection in doc["smart_collections"]:
            current = self.smart.get(collection["name"])
            if not current:
                current = self.app.json("POST", "/collections/smart", data=pick(collection, {"name", "description"}))
            smart_map[collection["id"]] = ident(current["id"])
        for collection in doc["smart_collections"]:
            self.write("PUT", f"/collections/smart/{smart_map[collection['id']]}",
                params={"is_public": str(collection["is_public"]).lower()},
                data={**pick(collection, {"name", "description"}), "filter_criteria": json.dumps(
                    self._translate_filters(collection["filter_criteria"], self.platform_map, self.collection_map, smart_map))})
        old, new = self.current_settings, doc["settings"]
        for section in ("platforms", "versions"):
            for key in old[section].keys() - new[section].keys():
                self.write("DELETE", f"/config/system/{section}/{quote(key, safe='')}")
            for key, value in new[section].items():
                self.write("POST", f"/config/system/{section}", json={"fs_slug": key, "slug": value})
        for key in EXCLUSIONS:
            for value in set(old["exclusions"][key]) - set(new["exclusions"][key]):
                self.write("DELETE", f"/config/exclude/{key}/{quote(value, safe='')}")
            for value in set(new["exclusions"][key]) - set(old["exclusions"][key]):
                self.write("POST", "/config/exclude", json={"exclusion_type": key, "exclusion_value": value})
        self.write("PUT", "/config/scan", json=new["scan"])
        self.write("PUT", "/config/converto_settings", json=new["converto"])
        profile = doc["profile"]
        if profile.get("avatar"):
            self.upload(f"/users/{self.user_id}", "avatar", profile["avatar"], data=pick(profile, {"email", "ra_username"}))
        else:
            self.write("PUT", f"/users/{self.user_id}", data=profile)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--url", help="Target RomM URL (defaults to Backuparr settings)")
    parser.add_argument("--api-key-file", help="Read a token from a file; never put a token in argv")
    parser.add_argument("--apply", action="store_true", help="Apply after full preflight (default: preview only)")
    args = parser.parse_args(argv)
    try:
        from config_store import load_config
        config = {} if args.api_key_file and args.url else load_config()["apps"]["romm"]
        token = Path(args.api_key_file).read_text().strip() if args.api_key_file else config["api_key"]
        app = RommApp(args.url or config["url"], token)
        with zipfile.ZipFile(args.archive) as archive:
            restore = RommRestore(app, archive)
            summary = restore.plan()
            print("Preflight passed:", json.dumps(summary))
            if args.apply:
                restore.apply()
                print("RomM API restore completed. See docs/romm.md for scope and timestamp limitations.")
            else:
                print("Preview only. Re-run with --apply to overwrite backed-up fields and upload assets.")
        return 0
    except (RommError, OSError, zipfile.BadZipFile) as exc:
        print(str(exc) if isinstance(exc, RommError) else "romm: could not read backup/configuration files", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
