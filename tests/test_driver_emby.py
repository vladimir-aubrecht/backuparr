from pathlib import Path

from apps.emby import EmbyApp
from backup import build_app
from config_store import restore_supported


def test_factory_settings_encryption_and_connection(authed_client, isolated_webui, monkeypatch):
    cfg = {"enabled": True, "url": "http://emby:8096", "api_key": "fixture-private-emby-key"}
    instance = build_app("emby", cfg)
    assert isinstance(instance, EmbyApp)
    assert instance.name == "emby"
    assert instance.session.headers["X-Emby-Token"] == cfg["api_key"]
    assert not restore_supported("emby")
    html = authed_client.get("/").data
    assert b'data-app="emby"' in html and b"API export:" in html
    assert authed_client.post("/api/config", json={"apps": {"emby": cfg}}).status_code == 200
    assert authed_client.get("/api/config").json["apps"]["emby"]["api_key"] == cfg["api_key"]
    assert cfg["api_key"] not in Path(isolated_webui.CONFIG_PATH).read_text()
    monkeypatch.setattr(EmbyApp, "test_connection", lambda self: "Administrator access verified")
    assert authed_client.post("/api/test/emby", json=cfg).json["ok"]
