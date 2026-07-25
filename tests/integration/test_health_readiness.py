"""Integration tests for MongoDB readiness."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_readiness_endpoint_reports_mongodb_connection() -> None:
    """The API should be ready when MongoDB responds."""

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["dependencies"]["mongodb"] == {
        "status": "ok",
        "detail": "MongoDB connection succeeded",
    }
