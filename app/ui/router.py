"""Server-rendered interface and safe operational actions."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pymongo.errors import PyMongoError

from app import __version__
from app.db.serialization import transaction_from_document
from app.models.quickbooks_sync import (
    QuickBooksSyncStatus,
)
from app.repositories.classification import (
    ClassificationRepository,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "app" / "templates"
STATIC_DIR = PROJECT_ROOT / "app" / "static"

LIVE_SYNC_CONFIRMATION = "BRIGHTFIX-SANDBOX-LIVE-SYNC"
SCRIPT_TIMEOUT_SECONDS = 360

templates = Jinja2Templates(
    directory=str(TEMPLATE_DIR),
)

router = APIRouter(
    include_in_schema=False,
)


@router.get(
    "/",
    response_class=HTMLResponse,
)
async def dashboard(
    request: Request,
) -> HTMLResponse:
    """Render the challenge workflow dashboard."""

    chart = request.app.state.chart_of_accounts

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "company_name": chart.company_name,
            "version": __version__,
            "accounts": tuple(account for account in chart.accounts if account.active),
        },
    )


@router.get("/ui/api/state")
async def dashboard_state(
    request: Request,
) -> dict[str, object]:
    """Return non-secret workflow evidence for the interface."""

    database = request.app.state.mongodb.database
    classification_repository: ClassificationRepository = (
        request.app.state.classification_repository
    )

    try:
        transaction_cursor = (
            database["normalized_transactions"]
            .find({})
            .sort(
                [
                    ("transaction_date", -1),
                    ("created_at", -1),
                ]
            )
            .limit(100)
        )

        transactions = [
            transaction_from_document(document) async for document in transaction_cursor
        ]

        classifications = await classification_repository.find_by_transaction_ids(
            tuple(transaction.id for transaction in transactions)
        )

        review_queue = await classification_repository.find_review_queue(limit=100)

        sync_collection = database["quickbooks_sync_records"]

        sync_counts = {
            sync_status.value: await sync_collection.count_documents(
                {
                    "status": sync_status.value,
                }
            )
            for sync_status in QuickBooksSyncStatus
        }

        total_sync_records = await sync_collection.count_documents({})

        counts = {
            "uploads": await database["upload_batches"].count_documents({}),
            "raw_records": await database["raw_records"].count_documents({}),
            "normalized_transactions": await database["normalized_transactions"].count_documents(
                {}
            ),
            "canonical_transactions": await database["normalized_transactions"].count_documents(
                {
                    "status": "valid",
                    "duplicate_of": None,
                }
            ),
            "duplicates": await database["normalized_transactions"].count_documents(
                {
                    "status": "duplicate",
                }
            ),
            "invalid": await database["normalized_transactions"].count_documents(
                {
                    "status": "invalid",
                }
            ),
            "classifications": await database["transaction_classifications"].count_documents({}),
            "approved": await database["transaction_classifications"].count_documents(
                {
                    "review_status": "approved",
                }
            ),
            "pending_review": await database["transaction_classifications"].count_documents(
                {
                    "review_status": "pending",
                }
            ),
            "quickbooks_connections": await database["quickbooks_connections"].count_documents({}),
            "quickbooks_sync_records": total_sync_records,
        }
    except PyMongoError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB dashboard evidence is unavailable",
        ) from exc

    transaction_rows = []

    for transaction in transactions:
        classification = classifications.get(transaction.id)
        decision = classification.decision if classification is not None else None
        counterparty = (
            decision.counterparty.normalized_name
            if (decision is not None and decision.counterparty is not None)
            else None
        )

        transaction_rows.append(
            {
                "id": str(transaction.id),
                "transaction_date": (
                    transaction.transaction_date.isoformat()
                    if transaction.transaction_date
                    else None
                ),
                "description": (
                    transaction.description_original
                    or transaction.description_normalized
                    or "No description"
                ),
                "amount": (str(transaction.amount) if transaction.amount is not None else None),
                "currency": transaction.currency,
                "bank_account": transaction.bank_account,
                "direction": (transaction.direction.value if transaction.direction else None),
                "record_status": transaction.status.value,
                "duplicate": (transaction.duplicate_of is not None),
                "classification": (
                    {
                        "version": classification.version,
                        "review_status": (classification.review_status.value),
                        "transaction_type": (decision.transaction_type.value),
                        "counterparty": counterparty,
                        "account_number": (decision.qbo_account.account_number),
                        "account_name": (decision.qbo_account.account_name),
                        "confidence": str(decision.confidence_score),
                        "source": decision.source.value,
                        "review_required": (decision.review_required),
                        "explanation": decision.explanation,
                    }
                    if classification is not None and decision is not None
                    else None
                ),
            }
        )

    return {
        "company_name": (request.app.state.chart_of_accounts.company_name),
        "gemini_enabled": (request.app.state.gemini_classifier is not None),
        "counts": counts,
        "sync_counts": sync_counts,
        "transactions": transaction_rows,
        "review_queue": [item.model_dump(mode="json") for item in review_queue],
        "sync_complete": (
            total_sync_records > 0
            and sync_counts[QuickBooksSyncStatus.SUCCEEDED.value] == total_sync_records
        ),
    }


@router.post("/ui/api/sync")
async def synchronize_quickbooks(
    confirmation: Annotated[
        str,
        Form(),
    ],
) -> dict[str, object]:
    """Run the guarded idempotent QBO sandbox sync."""

    if confirmation.strip() != LIVE_SYNC_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=("Type the exact sandbox confirmation phrase before synchronization"),
        )

    return await _run_project_script(
        "scripts/sync_qbo_sandbox.py",
        "--confirm",
        LIVE_SYNC_CONFIRMATION,
    )


@router.post("/ui/api/reconcile")
async def reconcile_quickbooks() -> dict[str, object]:
    """Run the read-only QBO cash-basis reconciliation."""

    return await _run_project_script(
        "scripts/reconcile_qbo_profit_and_loss.py",
    )


async def _run_project_script(
    *arguments: str,
) -> dict[str, object]:
    """Run one controlled project script without a shell."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *arguments,
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()

        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="The accounting operation timed out",
        ) from exc

    output = stdout.decode(
        "utf-8",
        errors="replace",
    )

    return {
        "success": process.returncode == 0,
        "exit_code": process.returncode,
        "output": output[-30000:],
    }
