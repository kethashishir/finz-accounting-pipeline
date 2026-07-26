"""Chart-of-accounts configuration loading."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.models.accounting import ChartOfAccountsConfig


class ChartOfAccountsConfigurationError(ValueError):
    """Raised when the accounting catalog cannot be safely loaded."""


def load_chart_of_accounts(path: Path) -> ChartOfAccountsConfig:
    """Load and strictly validate a JSON chart-of-accounts catalog."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ChartOfAccountsConfig.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ChartOfAccountsConfigurationError(
            f"Unable to load chart of accounts from {path}: {exc}"
        ) from exc
