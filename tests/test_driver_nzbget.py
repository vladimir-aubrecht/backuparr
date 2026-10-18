import json
from pathlib import Path

import pytest
import requests

from apps.nzbget import NzbgetApp, NzbgetError
from backup import build_app
from config_store import restore_supported


class Session:
    def __init__(self, result=None, status=200, error=None, payload=None):
        self.result = result if result is not None else [
            {"Name": "MainDir", "Value": "/downloads"},
            {"Name": "DestDir", "Value": "${MainDir}/complete"},
            {"Name": "Server1.Password", "Value": "fixture-server-password"},
            {"Name": "ControlPassword", "Value": "fixture-control-password"},
            {"Name": "Category1.Name", "Value": "TV"},
        ]
        self.status, self.error, self.payload = status, error, payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        method = kwargs['json']['method']
        result = self.result if method == 'loadconfig' else {'version': '25.4', 'status': {'DownloadLimit': 0}, 'config': self.result}.get(method, [])
        response._content = json.dumps(self.payload if self.payload is not None else {
            "version": "1.1", "id": 1, "result": result,
        }).encode()
        response._content_consumed = True
        return response


def driver(**kwargs):
    app = NzbgetApp("http://nzbget:6789/base/", "nzbget", "fixture-control-password")
    app.session = Session(**kwargs)
    return app


def test_connection_reads_saved_config_with_full_credentials_without_mutating_it():
    app = driver()
    assert "readable" in app.test_connection()
    url, kwargs = app.session.calls[0]
    assert url == "http://nzbget:6789/base/jsonrpc"
    assert kwargs["json"] == {"method": "loadconfig", "params": [], "id": 1}
    assert kwargs["allow_redirects"] is False
    assert "fixture-control-password" not in str(app.session.calls)
    assert NzbgetApp("http://nzbget:6789", "nzbget", "password").session.auth == ("nzbget", "password")


@pytest.mark.parametrize("username,password", [("", ""), ("control", "password"), ("control", ""), ("", "password")])
def test_connection_and_backup_send_native_auth_only_when_configured(tmp_path, monkeypatch, username, password):
    sent = []

    def send(_adapter, request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"id": 1, "result": [
            {"Name": "MainDir", "Value": "/downloads"},
            {"Name": "ControlPassword", "Value": password},
        ]}).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    app = build_app("nzbget", {"url": "http://nzbget:6789", "username": username, "api_key": password})
    assert "readable" in app.test_connection()
    app.backup(str(tmp_path))
    assert f"ControlPassword={password}\n" in (tmp_path / "nzbget.conf").read_text()
    assert len(sent) == 2
    for request in sent:
        assert json.loads(request.body)["method"] == "loadconfig"
        if username or password:
            assert request.headers["Authorization"] == requests.auth._basic_auth_str(username, password)
        else:
            assert "Authorization" not in request.headers


@pytest.mark.parametrize("credentials", [{}, {"username": "", "api_key": ""}])
def test_nzbget_can_be_enabled_saved_and_tested_without_native_credentials(authed_client, monkeypatch, credentials):
    cfg = {"enabled": True, "url": "http://nzbget:6789", **credentials}
    response = authed_client.post("/api/config", json={"apps": {"nzbget": cfg}})
    assert response.status_code == 200, response.json
    saved = authed_client.get("/api/config").json["apps"]["nzbget"]
    assert saved["enabled"] and saved["username"] == saved["api_key"] == ""
    sessions = []

    def check_config(app):
        sessions.append(app.session.auth)
        return [{"Name": "ControlPassword", "Value": ""}]

    monkeypatch.setattr(NzbgetApp, "_config", check_config)
    response = authed_client.post("/api/test/nzbget", json=cfg)
    assert response.status_code == 200 and response.json["ok"], response.json
    assert sessions == [None]


def test_backup_preserves_secrets_variables_categories_and_writes_restore_instructions(tmp_path):
    app = driver()
    out = tmp_path / "new-directory"
    assert app.backup(str(out)) == str(out)
    text = (out / "nzbget.conf").read_text()
    assert "DestDir=${MainDir}/complete\n" in text
    assert "Server1.Password=fixture-server-password\n" in text
    assert "ControlPassword=fixture-control-password\n" in text
    assert "Category1.Name=TV\n" in text
    assert (out / "nzbget.conf").stat().st_mode & 0o777 == 0o600
    assert "queue/history" in (out / "RESTORE.txt").read_text().lower()
    assert len(app.session.calls) == 1


@pytest.mark.parametrize("rows", [
    [], {}, [None], [{"Name": "MainDir", "Value": None}],
    [{"Name": "MainDir\nControlPassword", "Value": "secret"}],
    [{"Name": "MainDir", "Value": "a\nControlPassword=secret"}],
    [{"Name": "MainDir", "Value": "x"}, {"Name": "maindir", "Value": "y"}],
    [{"Name": "ControlPassword", "Value": "***"}],
])
def test_invalid_or_masked_config_never_becomes_a_backup(tmp_path, rows):
    with pytest.raises(NzbgetError):
        driver(result=rows).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 500])
def test_http_failures_are_actionable_and_do_not_create_an_archive(tmp_path, status):
    with pytest.raises(NzbgetError, match="nzbget:"):
        driver(status=status).backup(str(tmp_path))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("payload", [
    {"id": 1, "error": {"message": "secret response body"}},
    {"id": 2, "result": []}, {"result": []}, ["secret response body"],
])
def test_rpc_failure_does_not_expose_a_remote_response(payload):
    with pytest.raises(NzbgetError) as caught:
        driver(payload=payload).test_connection()
    assert "secret response body" not in str(caught.value)


def test_transport_errors_do_not_expose_credentials(caplog):
    with pytest.raises(NzbgetError) as caught:
        driver(error=requests.ConnectionError("http://user:fixture-control-password@host")).test_connection()
    assert "fixture-control-password" not in str(caught.value) + caplog.text
    assert caught.value.__suppress_context__


def test_factory_uses_the_encrypted_credential_field():
    app = build_app("nzbget", {"url": "http://nzbget:6789", "username": "control", "api_key": "pass"})
    assert app.session.auth == ("control", "pass")
    assert build_app("nzbget", {"url": "http://nzbget:6789"}).session.auth is None
    assert not restore_supported("nzbget")


def test_nzbget_settings_are_rendered_persisted_and_encrypted(authed_client, isolated_webui, monkeypatch):
    client = authed_client
    assert b"Control password" in client.get("/").data
    assert b"Control username" in client.get("/").data
    cfg = {"enabled": True, "url": "http://nzbget:6789", "username": "nzbget", "api_key": "private-nzbget-pass"}
    assert client.post("/api/config", json={"apps": {"nzbget": cfg}}).status_code == 200
    assert client.get("/api/config").json["apps"]["nzbget"]["username"] == "nzbget"
    assert client.get("/api/config").json["apps"]["nzbget"]["api_key"] == "private-nzbget-pass"
    assert "private-nzbget-pass" not in Path(isolated_webui.CONFIG_PATH).read_text()
    monkeypatch.setattr(NzbgetApp, "_config", lambda self: [])
    assert client.post("/api/test/nzbget", json=cfg).json["ok"]
