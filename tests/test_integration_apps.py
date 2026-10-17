"""The combined preview must retain each app's distinct settings and restore scope."""
from pathlib import Path


def test_new_apps_can_be_saved_together_without_losing_credentials_or_restore_scope(
    authed_client, isolated_webui,
):
    apps = {
        name: {"enabled": True, "url": f"http://{name}:{port}", "api_key": f"integration-{name}-secret"}
        for name, port in (("plex", 32400), ("jellyfin", 8096), ("emby", 8096), ("nzbget", 6789))
    }
    apps["plex"].update(restore_agent_url="http://agent:8991", restore_agent_token="integration-agent-secret")
    apps["nzbget"]["username"] = "control"
    assert authed_client.post("/api/config", json={"apps": apps}).status_code == 200
    # A later, partial edit must preserve every other app and the agent settings.
    assert authed_client.post("/api/config", json={"apps": {"emby": {"url": "http://emby-new:8096"}}}).status_code == 200
    apps["emby"]["url"] = "http://emby-new:8096"
    saved = authed_client.get("/api/config").json["apps"]
    raw = Path(isolated_webui.CONFIG_PATH).read_text()
    html = authed_client.get("/").get_data(as_text=True)
    meta = authed_client.get("/api/meta").json
    for name, fields in apps.items():
        for key, value in fields.items():
            assert saved[name][key] == value
        assert fields["api_key"] not in raw
        assert html.count(f'data-app="{name}"') == 1
        entry, = [item for item in meta if item["id"] == name]
        assert entry["restore_supported"] is (name == "plex")
    assert apps["plex"]["restore_agent_token"] not in raw
