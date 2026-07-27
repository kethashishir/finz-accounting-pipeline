"""API endpoints for internal cash-basis financial reports."""

from __future__ import annotations

from datetime import date
from typing import Annotated

import structlog
from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    status,
)
from pymongo.errors import PyMongoError

from app.models.accounting import ChartOfAccountsConfig
from app.models.profit_and_loss import ProfitAndLossReportSet
from app.repositories.reporting import (
    ProfitAndLossQueryError,
    ProfitAndLossRepository,
)
from app.services.reporting.profit_and_loss import (
    ProfitAndLossBuildError,
    generate_profit_and_loss_report_set,
)

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
)
logger = structlog.get_logger(__name__)


def _invalid_report_request(
    error: Exception,
) -> HTTPException:
    """Map reporting-domain validation errors to HTTP 422."""

    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": "invalid_profit_and_loss_request",
            "message": str(error),
        },
    )


def _database_unavailable() -> HTTPException:
    """Return the stable reporting database failure response."""

    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "mongodb_unavailable",
            "message": ("The Profit and Loss reporting database is unavailable"),
        },
    )


@router.get(
    "/profit-and-loss",
    response_model=ProfitAndLossReportSet,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": ("The requested P&L period or currency is invalid")
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "MongoDB is unavailable"},
    },
)
async def get_profit_and_loss(
    request: Request,
    start_date: Annotated[
        date,
        Query(description=("First day of the first reporting month")),
    ],
    end_date: Annotated[
        date,
        Query(description=("Last day of the final reporting month")),
    ],
    currency: Annotated[
        str,
        Query(
            min_length=3,
            max_length=3,
            pattern=r"^[A-Za-z]{3}$",
        ),
    ] = "USD",
) -> ProfitAndLossReportSet:
    """Return monthly and consolidated internal cash-basis P&Ls."""

    repository: ProfitAndLossRepository = request.app.state.profit_and_loss_repository
    chart_of_accounts: ChartOfAccountsConfig = request.app.state.chart_of_accounts

    try:
        report = await generate_profit_and_loss_report_set(
            source_reader=repository,
            start_date=start_date,
            end_date=end_date,
            currency=currency,
            chart_of_accounts=chart_of_accounts,
        )
    except (
        ProfitAndLossBuildError,
        ProfitAndLossQueryError,
    ) as exc:
        raise _invalid_report_request(exc) from exc
    except PyMongoError as exc:
        logger.exception(
            "profit_and_loss_database_failure",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            currency=currency,
        )
        raise _database_unavailable() from exc

    logger.info(
        "profit_and_loss_generated",
        start_date=report.consolidated.start_date.isoformat(),
        end_date=report.consolidated.end_date.isoformat(),
        currency=report.consolidated.currency,
        transaction_count=(report.consolidated.transaction_count),
        net_profit=str(report.consolidated.net_profit),
    )

    return report
