"""Result models for the complete ingestion workflow."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.ingestion import (
    NormalizedTransaction,
    UploadStatus,
)


class IngestionRecordCounts(BaseModel):
    """Record counts grouped by ingestion safety state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    physical: int = Field(ge=0)
    valid: int = Field(ge=0)
    invalid: int = Field(ge=0)
    duplicate: int = Field(ge=0)


class DuplicateCounts(BaseModel):
    """Duplicate counts grouped by where the canonical row was found."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    within_upload: int = Field(ge=0)
    across_uploads: int = Field(ge=0)
    source_id_conflicts: int = Field(ge=0)


class IngestionProcessResult(BaseModel):
    """API-safe result of processing and persisting an upload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_id: UUID
    source_file_name: str
    file_sha256: str
    status: UploadStatus
    counts: IngestionRecordCounts
    duplicates: DuplicateCounts
    warnings: list[str] = Field(default_factory=list)
    transactions: list[NormalizedTransaction]
