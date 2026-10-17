"""Read-only JSON export of the common Jellyfin/Emby HTTP API.

This is intentionally not a native database backup. API exports do not expose
password hashes, plugin databases or every server option.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import zipfile

import requests


class MediaServerError(RuntimeError):
    pass


class MediaServerApp:
    name = "media-server"
    label = "Media server"
    page_size = 500
    configuration_keys = ("encoding", "metadata", "branding", "livetv", "xbmcmetadata", "subtitles")
    plugin_configuration_filename_required = False
    playlist_duplicates_restorable = True
    metadata_fields = "Path,ProviderIds,SortName,DateCreated,ParentId,Overview,People,Genres,Studios,Taglines,MediaSources,MediaStreams,Chapters,DisplayPreferencesId,Tags,Settings,OriginalTitle,CustomRating,ProductionLocations,SpecialEpisodeNumbers,AirTime"
    # Folders and other containers need not carry UserData, but playable items do.
    playable_types = {"Movie", "Episode", "Audio", "Video", "MusicVideo", "AudioBook", "Book", "Trailer", "TvChannel"}

    def __init__(self, url, api_key, timeout=60):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and parsed.username is None and not parsed.query
                     and not parsed.fragment and parsed.port != 0)
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise MediaServerError(f"{self.name}: use an HTTP(S) server URL without credentials or query parameters")
        if not isinstance(api_key, str) or not api_key.strip():
            raise MediaServerError(f"{self.name}: an administrator API key is required")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Emby-Token": api_key, "Accept": "application/json"})

    def _get(self, path, expected, *, unavailable=None, **params):
        try:
            response = self.session.get(
                self.url + path, params=params, timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise MediaServerError(f"{self.name}: could not reach the server; check its URL and network access") from None
        with response:
            status = response.status_code
            if unavailable is not None and status in (404, 405, 501):
                unavailable.append({"endpoint": path, "status": status, "reason": "Not exposed by this server/version/plugin"})
                return None
            if status in (401, 403):
                raise MediaServerError(f"{self.name}: access denied; create an administrator API key in the server dashboard")
            if 300 <= status < 400:
                raise MediaServerError(f"{self.name}: redirect refused; use the final internal server URL")
            if status != 200:
                raise MediaServerError(f"{self.name}: HTTP {status} reading {path}; no complete export was created")
            try:
                data = response.json()
            except ValueError:
                raise MediaServerError(f"{self.name}: expected JSON from {path}; check the URL and proxy") from None
        if not isinstance(data, expected):
            raise MediaServerError(f"{self.name}: unexpected response from {path}")
        return data

    def _users(self):
        users = self._get("/Users", list)
        ids = set()
        for user in users:
            user_id = user.get("Id") if isinstance(user, dict) else None
            if (not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", user_id)
                    or user_id in ids or not isinstance(user.get("Policy"), dict)
                    or not isinstance(user.get("Configuration"), dict)):
                raise MediaServerError(f"{self.name}: invalid or incomplete user list; administrator access is required")
            ids.add(user_id)
        if not users:
            raise MediaServerError(f"{self.name}: no users returned; finish server setup and check API permissions")
        return users

    def _settings(self):
        server = self._get("/System/Info", dict)
        config = self._get("/System/Configuration", dict)
        if not server.get("Version") or not server.get("Id") or not config:
            raise MediaServerError(f"{self.name}: incomplete server settings response")
        return server, config

    def test_connection(self):
        self._settings()
        self._users()
        return f"{self.label} administrator access verified; limited API export, not a full database backup"

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
            raise MediaServerError("Invalid identifier returned by the media server")
        return value

    def _items(self, user_id=None, path="/Items", playlist=False, **extra):
        seen = set()
        media_ids = set()
        total, offset = None, 0
        params = {
            "Recursive": "true", "SortBy": "SortName", "SortOrder": "Ascending",
            "EnableUserData": "true" if user_id else "false", "EnableImages": "true",
            "EnableTotalRecordCount": "true", "Fields": self.metadata_fields,
        }
        if user_id:
            params["UserId"] = user_id
            # UI defaults can replace movies with collections after paging, hiding data.
            params["CollapseBoxSetItems"] = "false"
            params["GroupItemsIntoCollections"] = "false"
        params.update(extra)
        if playlist:
            # Playlist order and repeated entries matter; never sort by title.
            for key in ("Recursive", "SortBy", "SortOrder"):
                params.pop(key, None)
        while True:
            page = self._get(path, dict, StartIndex=offset, Limit=self.page_size, **params)
            count, items = page.get("TotalRecordCount"), page.get("Items")
            if (type(count) is not int or count < 0 or not isinstance(items, list)
                    or (total is not None and count != total)):
                raise MediaServerError(f"{self.name}: invalid or changing library pagination; retry when scans are idle")
            total = count
            for item in items:
                item_id = item.get("Id") if isinstance(item, dict) else None
                self._id(item_id)
                # Jellyfin can reuse the media ID as PlaylistItemId for repeated tracks.
                # A playlist is an ordered sequence; identical entries are legitimate.
                entry_id = str(offset) if playlist else item_id
                if not isinstance(entry_id, str) or entry_id in seen:
                    raise MediaServerError(f"{self.name}: duplicate item/page; library changed during export")
                if user_id and item.get("Type") in self.playable_types:
                    state = item.get("UserData")
                    if not isinstance(state, dict) or type(state.get("Played")) is not bool:
                        raise MediaServerError(f"{self.name}: a playable item is missing its user's watched state")
                if playlist and not self.playlist_duplicates_restorable and item_id in media_ids:
                    raise MediaServerError(f"{self.name}: this playlist repeats an item; this server API cannot restore repeated entries")
                media_ids.add(item_id)
                seen.add(entry_id)
                offset += 1
                yield item
            if offset == total:
                return
            if not items or offset > total:
                raise MediaServerError(f"{self.name}: incomplete library listing; no complete export was created")

    def _image(self, zf, path, filename):
        try:
            response = self.session.get(self.url + path, timeout=self.timeout, allow_redirects=False, stream=True)
            with response:
                if response.status_code != 200 or not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    raise MediaServerError(f"{self.name}: could not export artwork (HTTP {response.status_code})")
                # Keep the original bytes. The image manifest records type/index and MIME type.
                with zf.open(filename, "w", force_zip64=True) as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
                return response.headers["Content-Type"]
        except requests.RequestException:
            raise MediaServerError(f"{self.name}: artwork download interrupted; retry the backup") from None

    def _extras(self, zf, users, unavailable):
        for key in self.configuration_keys:
            data = self._get(f"/System/Configuration/{key}", dict, unavailable=unavailable)
            if data is not None:
                self._write_json(zf, f"configuration/{key}.json", data)
        for path, filename, expected in (
            ("/ScheduledTasks", "scheduled-tasks.json", list),
            ("/LiveTv/Timers", "live-tv/timers.json", dict),
            ("/LiveTv/SeriesTimers", "live-tv/series-timers.json", dict),
        ):
            data = self._get(path, expected, unavailable=unavailable)
            if data is not None:
                self._write_json(zf, filename, data)
        plugins = self._get("/Plugins", list)
        self._write_json(zf, "plugins.json", plugins)
        for plugin in plugins:
            plugin_id = self._id(plugin.get("Id") if isinstance(plugin, dict) else None)
            if self.plugin_configuration_filename_required and not plugin.get("ConfigurationFileName"):
                unavailable.append({"endpoint": f"/Plugins/{plugin_id}/Configuration", "reason": "Plugin does not expose a standard configuration file"})
                continue
            data = self._get(f"/Plugins/{plugin_id}/Configuration", dict, unavailable=unavailable)
            if data is not None:
                self._write_json(zf, f"plugin-configuration/{plugin_id}.json", data)
        for user in users:
            user_id = self._id(user["Id"])
            # The web clients use the legacy 'emby' client namespace for these preferences.
            data = self._get("/DisplayPreferences/usersettings", dict, UserId=user_id, Client="emby", unavailable=unavailable)
            if data is not None:
                self._write_json(zf, f"display-preferences/{user_id}.json", data)
            if user.get("PrimaryImageTag"):
                mime = self._image(zf, f"/Users/{user_id}/Images/Primary", f"user-images/{user_id}")
                self._write_json(zf, f"user-images/{user_id}.json", {"content_type": mime})

    @staticmethod
    def _write_json(zf, name, data):
        zf.writestr(name, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def backup(self, dest_dir):
        server, config = self._settings()
        users = self._users()
        libraries = self._get("/Library/VirtualFolders", list)
        if not all(isinstance(library, dict) for library in libraries):
            raise MediaServerError(f"{self.name}: invalid library configuration")
        os.makedirs(dest_dir, exist_ok=True)
        archive = Path(dest_dir) / f"{self.name}-api-export.zip"
        complete = False
        try:
            with archive.open("wb") as output:
                os.chmod(archive, 0o600)
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for filename, data in (("server.json", server), ("system-configuration.json", config),
                                           ("libraries.json", libraries), ("users.json", users)):
                        self._write_json(zf, filename, data)
                    unavailable = []
                    self._extras(zf, users, unavailable)
                    artwork = set()
                    memberships = set()
                    catalog_count = 0
                    with zf.open("catalog.jsonl", "w", force_zip64=True) as stream:
                        for item in self._items():
                            stream.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                            catalog_count += 1
                            if item.get("Type") == "BoxSet":
                                admin = next((u for u in users if u["Policy"].get("IsAdministrator")), None)
                                if admin is None:
                                    raise MediaServerError("An administrator user is required to export collections")
                                memberships.add((admin["Id"], self._id(item["Id"]), "BoxSet"))
                            if item.get("ImageTags") or item.get("BackdropImageTags") or item.get("ScreenshotImageTags"):
                                artwork.add(self._id(item["Id"]))
                    counts = {}
                    for user in users:
                        user_id = user["Id"]
                        counts[user_id] = 0
                        # JSON Lines avoids accumulating full item objects for large libraries.
                        with zf.open(f"user-items/{user_id}.jsonl", "w", force_zip64=True) as stream:
                            for item in self._items(user_id):
                                stream.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                                counts[user_id] += 1
                                if item.get("Type") in ("Playlist", "BoxSet"):
                                    memberships.add((user_id, self._id(item["Id"]), item["Type"]))
                                if item.get("ImageTags") or item.get("BackdropImageTags") or item.get("ScreenshotImageTags"):
                                    artwork.add(self._id(item["Id"]))
                    for user_id, item_id, kind in sorted(memberships):
                        members = (self._items(user_id, path=f"/Playlists/{item_id}/Items", playlist=True) if kind == "Playlist"
                                   else self._items(user_id, ParentId=item_id, Recursive="false"))
                        with zf.open(f"memberships/{user_id}/{item_id}.jsonl", "w", force_zip64=True) as stream:
                            for item in members:
                                stream.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                    for item_id in sorted(artwork):
                        images = self._get(f"/Items/{item_id}/Images", list)
                        for info in images:
                            image_type = self._id(info.get("ImageType") if isinstance(info, dict) else None)
                            index = info.get("ImageIndex", 0)
                            if type(index) is not int or index < 0:
                                raise MediaServerError(f"{self.name}: invalid artwork index")
                            info["ExportFile"] = f"images/{item_id}/{image_type}-{index}"
                            info["ContentType"] = self._image(zf, f"/Items/{item_id}/Images/{image_type}/{index}", info["ExportFile"])
                        self._write_json(zf, f"images/{item_id}/index.json", images)
                    limitations = [
                        "API export only; not a native backup or a complete database snapshot.",
                        "No user passwords/hashes, API-key/device registrations, login sessions, plugin binaries/databases, or settings not exposed by the listed API endpoints.",
                        "Artwork and metadata are exported through the API, not as native metadata-directory files. No media, subtitle files, trickplay files or cache.",
                        "No recording media or complete historical playback events; only current per-item user state. Non-web-client display preferences are not enumerable.",
                        "Per-user items cover only content currently visible to that user; deleted or inaccessible items are excluded.",
                        "Reads are sequential, not atomic; avoid library scans and configuration changes during export.",
                        "Restore: recreate users with new passwords, libraries and empty playlists/collections, then use python -m apps.media_restore (preview first, --apply to restore).",
                    ]
                    self._write_json(zf, "manifest.json", {
                        "format": "backuparr-media-api-export", "format_version": 1,
                        "app": self.name, "created_at": datetime.now(timezone.utc).isoformat(),
                        "server_id": server["Id"], "server_version": server["Version"],
                        "catalog_item_count": catalog_count, "user_item_counts": counts,
                        "configuration_keys": list(self.configuration_keys), "unavailable": unavailable, "limitations": limitations,
                    })
                    zf.writestr("RESTORE.txt", self._restore_notes(limitations))
            complete = True
        finally:
            if not complete:
                archive.unlink(missing_ok=True)
        return str(archive)

    def _restore_notes(self, limitations):
        return (
            f"{self.label}: LIMITED API EXPORT\n\n"
            "This ZIP cannot be imported by the server's native backup/restore UI.\n"
            "Do not copy these JSON files over server configuration files or databases.\n\n"
            "Contents:\n"
            "- system-configuration.json: core settings from GET /System/Configuration.\n"
            "- libraries.json: library paths/options from GET /Library/VirtualFolders.\n"
            "- users.json: user names, IDs, preferences and policies from GET /Users.\n"
            "- configuration/, plugin-configuration/, plugins.json: exposed settings and versions.\n"
            "- scheduled-tasks.json, live-tv/: task triggers and recording schedules.\n"
            "- catalog.jsonl: all items visible to the administrator with extended metadata.\n"
            "- memberships/: ordered playlist entries and collection members per user.\n"
            "- images/, user-images/: original API artwork bytes with MIME/type/index manifests.\n"
            "- display-preferences/: web-client preferences.\n"
            "- user-items/<old-user-id>.jsonl: one item per line, including UserData\n"
            "  (Played, PlayCount, PlaybackPositionTicks, favorites and last-played time\n"
            "  where returned), item IDs, paths and provider IDs for matching.\n\n"
            "Recovery using the included CLI (see README for full instructions):\n"
            "1. Set up the same server version and reconnect the original media.\n"
            "2. Recreate users/passwords and libraries using users.json and libraries.json.\n"
            "3. Review core settings and paths before applying them in the dashboard or\n"
            "   POST /System/Configuration. Named settings and plugin configuration are in\n"
            "   configuration/ and plugin-configuration/; reinstall compatible plugins first.\n"
            "4. Match users by name and items by ProviderIds/Path (plus episode identity),\n"
            "   not old IDs. The included restore command performs this mapping before any writes.\n"
            "   Restore Played=true and Played=false distinctly; preserve zero positions.\n"
            "   Use python -m apps.media_restore ARCHIVE.zip --url URL --key-file KEY_FILE\n"
            "   to preview the restoration plan; append --apply to restore. See README\n"
            "   for setup and --path-map. Recreate playlist ownership/sharing yourself;\n"
            "   the sole administrator must be able to edit playlists, or specify\n"
            "   --playlist-owner OLD_PLAYLIST_ID=USER_NAME. Restore overwrites target data\n"
            "   and has no automatic rollback.\n"
            "5. Live TV: reconnect tuners/guide and recreate future series/recordings in\n"
            "   the dashboard from live-tv/ JSON. The CLI does not recreate timers.\n"
            "   API alternative: POST /LiveTv/SeriesTimers then /LiveTv/Timers, replacing\n"
            "   ChannelId/ProgramId with current guide IDs and omitting old IDs/status.\n\n"
            "Limitations:\n" + "".join(f"- {line}\n" for line in limitations)
            + "\nCheck manifest.json unavailable for endpoints this server did not expose.\n"
            "Export can include plugin credentials; protect the archive.\n"
        )
