"""Integration tests for the complete ingestion API."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.synchronous.database import Database

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def ingestion_client() -> Iterator[tuple[TestClient, Database[dict[str, Any]]]]:
    """Run the API against an isolated MongoDB database."""

    database_name = f"finz_accounting_api_test_{uuid4().hex}"
    settings = Settings(mongodb_database=database_name)
    cleanup_client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongodb_uri,
        uuidRepresentation="standard",
    )
    database = cleanup_client[database_name]

    try:
        with TestClient(create_app(settings)) as client:
            yield client, database
    finally:
        cleanup_client.drop_database(database_name)
        cleanup_client.close()


def config_json() -> str:
    """Return a mapping for a deliberately reordered CSV."""

    return json.dumps(
        {
            "file_type": "csv",
            "header_row": 1,
            "date_format": "%Y-%m-%d",
            "column_mapping": {
                "source_transaction_id": "Transaction ID",
                "transaction_date": "Date",
                "description": "Description",
                "amount": "Amount",
                "currency": "Currency",
                "bank_account": "Account",
            },
        }
    )


def source_content() -> bytes:
    """Return valid, duplicate, and invalid physical records."""

    return (
        b"Description,Account,Currency,Amount,Date,Transaction ID\n"
        b"Customer receipt,Operating Checking,USD,1250.00,"
        b"2026-04-01,BF-1\n"
        b"Customer receipt,Operating Checking,USD,1250.00,"
        b"2026-04-01,BF-1\n"
        b"Broken amount,Operating Checking,USD,not-money,"
        b"2026-04-02,BF-2\n"
    )


def post_source(client: TestClient):
    """Submit the configured source to the processing endpoint."""

    return client.post(
        "/api/v1/ingestion/process",
        data={"config_json": config_json()},
        files={
            "file": (
                "bank.csv",
                source_content(),
                "text/csv",
            )
        },
    )


def test_process_endpoint_runs_complete_ingestion(
    ingestion_client: tuple[
        TestClient,
        Database[dict[str, Any]],
    ],
) -> None:
    """The API normalizes, detects duplicates, and persists all rows."""

    client, database = ingestion_client
    response = post_source(client)

    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "completed_with_errors"
    assert body["source_file_name"] == "bank.csv"
    assert body["counts"] == {
        "physical": 3,
        "valid": 1,
        "invalid": 1,
        "duplicate": 1,
    }
    assert body["duplicates"] == {
        "within_upload": 1,
        "across_uploads": 0,
        "source_id_conflicts": 0,
    }
    assert [transaction["status"] for transaction in body["transactions"]] == [
        "valid",
        "duplicate",
        "invalid",
    ]

    upload_id = UUID(body["upload_id"])
    assert database.upload_batches.count_documents({"_id": upload_id}) == 1
    assert database.raw_records.count_documents({"upload_id": upload_id}) == 3
    assert database.normalized_transactions.count_documents({"upload_id": upload_id}) == 3


def test_process_endpoint_rejects_identical_file(
    ingestion_client: tuple[
        TestClient,
        Database[dict[str, Any]],
    ],
) -> None:
    """Re-uploading the exact file returns its existing upload ID."""

    client, database = ingestion_client

    first = post_source(client)
    second = post_source(client)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "duplicate_file_upload"
    assert second.json()["detail"]["existing_upload_id"] == first.json()["upload_id"]
    assert database.upload_batches.count_documents({}) == 1
    assert database.raw_records.count_documents({}) == 3


def test_process_endpoint_rejects_invalid_configuration(
    ingestion_client: tuple[
        TestClient,
        Database[dict[str, Any]],
    ],
) -> None:
    """Invalid mapping configuration creates no persistence records."""

    client, database = ingestion_client

    response = client.post(
        "/api/v1/ingestion/process",
        data={"config_json": '{"file_type": "csv"}'},
        files={
            "file": (
                "bank.csv",
                source_content(),
                "text/csv",
            )
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_ingestion_config"
    assert database.upload_batches.count_documents({}) == 0
    assert database.raw_records.count_documents({}) == 0
    assert database.normalized_transactions.count_documents({}) == 0
