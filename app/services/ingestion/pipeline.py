"""End-to-end orchestration for one ingestion request."""

from __future__ import annotations

from uuid import uuid4

from app.models.ingestion import (
    IngestionConfig,
    RecordStatus,
    UploadBatch,
    UploadStatus,
)
from app.models.ingestion_result import (
    DuplicateCounts,
    IngestionProcessResult,
    IngestionRecordCounts,
)
from app.repositories.ingestion import (
    DuplicateFileUploadError,
    IngestionRepository,
)
from app.services.ingestion.duplicate_detector import DuplicateDetector
from app.services.ingestion.normalizer import SourceFileNormalizer


class IngestionPipeline:
    """Coordinate normalization, duplicate detection, and persistence."""

    def __init__(
        self,
        repository: IngestionRepository,
        *,
        normalizer: SourceFileNormalizer | None = None,
        duplicate_detector: DuplicateDetector | None = None,
    ) -> None:
        self.repository = repository
        self.normalizer = normalizer or SourceFileNormalizer()
        self.duplicate_detector = duplicate_detector or DuplicateDetector()

    async def process(
        self,
        *,
        file_name: str,
        content: bytes,
        config: IngestionConfig,
    ) -> IngestionProcessResult:
        """Process and persist one mapped source file."""

        await self.repository.ensure_indexes()

        upload_id = uuid4()
        normalized = self.normalizer.normalize(
            upload_id=upload_id,
            file_name=file_name,
            content=content,
            config=config,
        )

        existing_upload = await self.repository.find_upload_by_hash(
            normalized.inspection.file_sha256
        )
        if existing_upload is not None:
            raise DuplicateFileUploadError(existing_upload.id)

        existing_transactions = await self.repository.find_existing_transactions(
            normalized.transactions
        )

        detection = self.duplicate_detector.detect(
            normalized.transactions,
            existing_transactions=existing_transactions,
        )
        transactions = list(detection.transactions)

        valid_count = sum(transaction.status == RecordStatus.VALID for transaction in transactions)
        invalid_count = sum(
            transaction.status == RecordStatus.INVALID for transaction in transactions
        )
        duplicate_count = sum(
            transaction.status == RecordStatus.DUPLICATE for transaction in transactions
        )

        upload_status = (
            UploadStatus.COMPLETED_WITH_ERRORS if invalid_count else UploadStatus.COMPLETED
        )

        upload = UploadBatch(
            id=upload_id,
            source_file_name=normalized.inspection.safe_file_name,
            file_type=normalized.inspection.file_type,
            file_sha256=normalized.inspection.file_sha256,
            status=upload_status,
            config=config,
            physical_record_count=len(normalized.raw_records),
        )

        await self.repository.save_batch(
            upload=upload,
            raw_records=normalized.raw_records,
            transactions=transactions,
        )

        return IngestionProcessResult(
            upload_id=upload.id,
            source_file_name=upload.source_file_name,
            file_sha256=upload.file_sha256,
            status=upload.status,
            counts=IngestionRecordCounts(
                physical=len(transactions),
                valid=valid_count,
                invalid=invalid_count,
                duplicate=duplicate_count,
            ),
            duplicates=DuplicateCounts(
                within_upload=detection.within_upload_count,
                across_uploads=detection.cross_upload_count,
                source_id_conflicts=detection.conflict_count,
            ),
            warnings=normalized.inspection.warnings,
            transactions=transactions,
        )
