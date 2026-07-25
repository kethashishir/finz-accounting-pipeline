"""API endpoints for inspecting and processing source uploads."""

from typing import Annotated

import structlog
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.models.source import SourceFileInspection
from app.services.ingestion.inspector import (
    SourceFileInspector,
    SourceInspectionError,
    SourceInspectionErrorCode,
)

router = APIRouter(prefix="/ingestion", tags=["ingestion"])
logger = structlog.get_logger(__name__)


@router.post(
    "/inspect",
    response_model=SourceFileInspection,
    responses={
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "The uploaded file exceeds the allowed size"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The uploaded file cannot be inspected safely"
        },
    },
)
async def inspect_source_file(
    file: Annotated[UploadFile, File(description="CSV or XLSX bank export")],
) -> SourceFileInspection:
    """Inspect an untrusted source without persisting or processing it."""

    inspector = SourceFileInspector()

    try:
        content = await file.read(inspector.max_file_bytes + 1)
        inspection = inspector.inspect(
            file_name=file.filename or "",
            content=content,
        )
    except SourceInspectionError as exc:
        response_status = (
            status.HTTP_413_CONTENT_TOO_LARGE
            if exc.code == SourceInspectionErrorCode.FILE_TOO_LARGE
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )

        logger.warning(
            "source_file_inspection_rejected",
            error_code=exc.code.value,
            file_name=file.filename,
        )

        raise HTTPException(
            status_code=response_status,
            detail={
                "code": exc.code.value,
                "message": exc.message,
            },
        ) from exc
    finally:
        await file.close()

    logger.info(
        "source_file_inspected",
        file_name=inspection.safe_file_name,
        file_type=inspection.file_type.value,
        size_bytes=inspection.size_bytes,
        worksheet_count=len(inspection.sheets),
    )

    return inspection
