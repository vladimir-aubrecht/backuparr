"""Limited Jellyfin backup through read-only API exports."""
from apps.media_server import MediaServerApp


class JellyfinApp(MediaServerApp):
    playlist_duplicates_restorable = False
    name = "jellyfin"
    label = "Jellyfin"
    configuration_keys = ("encoding", "metadata", "network", "branding", "livetv", "xbmcmetadata", "database", "subtitles")
