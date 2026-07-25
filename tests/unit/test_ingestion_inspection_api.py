"""Tests for the source-file inspection API."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_inspection_endpoint_returns_csv_structure() -> None:
    """A valid CSV returns safe mapping metadata and preview rows."""

    content = (
        b"Date,Description,Amount,Currency,Account\n"
        b"2026-04-01,Customer receipt,1250.00,USD,"
        b"Operating Checking\n"
    )

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/ingestion/inspect",
            files={
                "file": (
                    "../../bank-export.csv",
                    content,
                    "text/csv",
                )
            },
        )

    assert response.status_code == 200

    body = response.json()
    assert body["original_file_name"] == "../../bank-export.csv"
    assert body["safe_file_name"] == "bank-export.csv"
    assert body["file_type"] == "csv"
    assert body["size_bytes"] == len(content)
    assert len(body["file_sha256"]) == 64
    assert body["encoding"] == "utf-8-sig"
    assert body["delimiter"] == ","
    assert body["sheets"] == [
        {
            "name": "CSV",
            "row_count": 2,
            "column_count": 5,
            "preview_rows": [
                {
                    "row_number": 1,
                    "values": [
                        "Date",
                        "Description",
                        "Amount",
                        "Currency",
                        "Account",
                    ],
                },
                {
                    "row_number": 2,
                    "values": [
                        "2026-04-01",
                        "Customer receipt",
                        "1250.00",
                        "USD",
                        "Operating Checking",
                    ],
                },
            ],
        }
    ]


def test_inspection_endpoint_rejects_unsupported_extension() -> None:
    """An unsupported source type returns a stable validation error."""

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/ingestion/inspect",
            files={
                "file": (
                    "bank-export.txt",
                    b"not a supported bank export",
                    "text/plain",
                )
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "unsupported_extension",
        "message": "Only .csv and .xlsx source files are supported",
    }


def test_inspection_endpoint_rejects_oversized_file() -> None:
    """The API rejects input beyond the inspector's configured limit."""

    oversized = b"x" * (10 * 1024 * 1024 + 1)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/ingestion/inspect",
            files={
                "file": (
                    "oversized.csv",
                    oversized,
                    "text/csv",
                )
            },
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "file_too_large"
    assert "10485760 bytes" in response.json()["detail"]["message"]
