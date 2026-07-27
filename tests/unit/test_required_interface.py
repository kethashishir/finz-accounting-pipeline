"""Focused smoke test for the required challenge interface."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_required_interface_and_static_assets_render() -> None:
    """The complete workflow shell is served locally."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database=(f"finz_ui_smoke_{uuid4().hex[:12]}"),
        gemini_api_key=None,
        gemini_model=None,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/")

        assert response.status_code == 200
        assert "Upload and map bank data" in response.text
        assert "Classification review" in response.text
        assert "Cash-basis Profit &amp; Loss" in response.text
        assert "QuickBooks reconciliation" in response.text

        stylesheet = client.get("/static/app.css")
        javascript = client.get("/static/app.js")

        assert stylesheet.status_code == 200
        assert javascript.status_code == 200
        assert ".app-shell" in stylesheet.text
        assert "runReconciliation" in javascript.text
