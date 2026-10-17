from pathlib import Path

from apps.jellyfin import JellyfinApp
from backup import build_app
from config_store import restore_supported


def test_factory_settings_encryption_and_connection(authed_client, isolated_webui, monkeypatch):
    cfg = {"enabled": True, "url": "http://jellyfin:8096", "api_key": "fixture-private-jellyfin-key"}
    instance = build_app("jellyfin", cfg)
    assert isinstance(instance, JellyfinApp)
    assert instance.name == "jellyfin"
    assert instance.session.headers["X-Emby-Token"] == cfg["api_key"]
    assert not restore_supported("jellyfin")
    html = authed_client.get("/").data
    assert b'data-app="jellyfin"' in html and b"API export:" in html
    assert authed_client.post("/api/config", json={"apps": {"jellyfin": cfg}}).status_code == 200
    assert authed_client.get("/api/config").json["apps"]["jellyfin"]["api_key"] == cfg["api_key"]
    assert cfg["api_key"] not in Path(isolated_webui.CONFIG_PATH).read_text()
    monkeypatch.setattr(JellyfinApp, "test_connection", lambda self: "Administrator access verified")
    assert authed_client.post("/api/test/jellyfin", json=cfg).json["ok"]
