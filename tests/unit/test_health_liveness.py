"""Unit tests for the liveness endpoint."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_liveness_endpoint_reports_application_metadata() -> None:
    """The API should report that the process is alive."""

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live")

    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "Finz Accounting Pipeline"
    assert body["environment"] == "development"
    assert body["version"] == "0.1.0"
    assert body["timestamp"]
