"""Limited Emby backup through read-only API exports."""
from apps.media_server import MediaServerApp


class EmbyApp(MediaServerApp):
    name = "emby"
    label = "Emby"
    configuration_keys = ("encoding", "metadata", "branding", "livetv", "xbmcmetadata", "dlna")
    plugin_configuration_filename_required = True
