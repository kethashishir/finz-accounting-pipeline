"""Models returned by source-file inspection."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.ingestion import FileType


class SourcePreviewRow(BaseModel):
    """One physical source row shown during mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=1)
    values: list[Any]


class SourceSheetPreview(BaseModel):
    """Metadata and preview rows for one worksheet-like source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    preview_rows: list[SourcePreviewRow]


class SourceFileInspection(BaseModel):
    """Safe structural inspection of an uploaded source file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    original_file_name: str = Field(min_length=1)
    safe_file_name: str = Field(min_length=1)
    file_type: FileType
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)

    encoding: str | None = None
    delimiter: str | None = None

    sheets: list[SourceSheetPreview]
    warnings: list[str] = Field(default_factory=list)
