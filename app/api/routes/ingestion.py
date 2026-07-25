"""API endpoints for inspecting and processing source uploads."""

import json
from json import JSONDecodeError
from typing import Annotated, Any

import structlog
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from app.models.ingestion import IngestionConfig
from app.models.ingestion_result import IngestionProcessResult
from app.models.source import SourceFileInspection
from app.repositories.ingestion import (
    DuplicateFileUploadError,
    IngestionRepository,
    PersistenceConflictError,
)
from app.services.ingestion.inspector import (
    SourceFileInspector,
    SourceInspectionError,
    SourceInspectionErrorCode,
)
from app.services.ingestion.normalizer import (
    SourceNormalizationError,
)
from app.services.ingestion.pipeline import IngestionPipeline

router = APIRouter(prefix="/ingestion", tags=["ingestion"])
logger = structlog.get_logger(__name__)


async def _read_upload(file: UploadFile) -> bytes:
    """Read only enough bytes to enforce the source-size limit."""

    inspector = SourceFileInspector()
    return await file.read(inspector.max_file_bytes + 1)


def _configuration_issues(
    error: ValidationError,
) -> list[dict[str, str]]:
    """Return safe configuration errors without echoing input values."""

    return [
        {
            "field": ".".join(str(part) for part in item["loc"]),
            "message": item["msg"],
        }
        for item in error.errors()
    ]


def _source_error_detail(
    error: SourceInspectionError | SourceNormalizationError,
) -> dict[str, str]:
    """Return a stable error payload for source-data failures."""

    return {
        "code": error.code.value,
        "message": error.message,
    }


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
    file: Annotated[
        UploadFile,
        File(description="CSV or XLSX bank export"),
    ],
) -> SourceFileInspection:
    """Inspect an untrusted source without persisting it."""

    inspector = SourceFileInspector()

    try:
        content = await _read_upload(file)
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
            detail=_source_error_detail(exc),
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


@router.post(
    "/process",
    response_model=IngestionProcessResult,
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "The file or persistence identity already exists"
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "The uploaded file exceeds the allowed size"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "The mapping or source data is invalid"
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "MongoDB is unavailable"},
    },
)
async def process_source_file(
    request: Request,
    file: Annotated[
        UploadFile,
        File(description="CSV or XLSX bank export"),
    ],
    config_json: Annotated[
        str,
        Form(description="JSON-encoded ingestion configuration"),
    ],
) -> IngestionProcessResult:
    """Run the complete mapped ingestion workflow."""

    try:
        try:
            config_data: Any = json.loads(config_json)
            config = IngestionConfig.model_validate(config_data)
        except JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_config_json",
                    "message": "Ingestion configuration is not valid JSON",
                },
            ) from exc
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_ingestion_config",
                    "message": "Ingestion configuration is invalid",
                    "issues": _configuration_issues(exc),
                },
            ) from exc

        content = await _read_upload(file)
        repository: IngestionRepository = request.app.state.ingestion_repository
        pipeline = IngestionPipeline(repository)

        result = await pipeline.process(
            file_name=file.filename or "",
            content=content,
            config=config,
        )
    except SourceInspectionError as exc:
        response_status = (
            status.HTTP_413_CONTENT_TOO_LARGE
            if exc.code == SourceInspectionErrorCode.FILE_TOO_LARGE
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(
            status_code=response_status,
            detail=_source_error_detail(exc),
        ) from exc
    except SourceNormalizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_source_error_detail(exc),
        ) from exc
    except DuplicateFileUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_file_upload",
                "message": str(exc),
                "existing_upload_id": str(exc.existing_upload_id),
            },
        ) from exc
    except PersistenceConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "persistence_conflict",
                "message": str(exc),
            },
        ) from exc
    except PyMongoError as exc:
        logger.exception("ingestion_database_failure")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "mongodb_unavailable",
                "message": "The ingestion database is unavailable",
            },
        ) from exc
    finally:
        await file.close()

    logger.info(
        "source_file_processed",
        upload_id=str(result.upload_id),
        file_name=result.source_file_name,
        physical_count=result.counts.physical,
        invalid_count=result.counts.invalid,
        duplicate_count=result.counts.duplicate,
    )

    return result
