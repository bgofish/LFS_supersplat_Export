"""Domain errors raised by the SuperSplat plugin."""

from __future__ import annotations

from typing import Any


class SuperSplatError(Exception):
    """Base class for user-facing plugin errors."""


class ConfigurationError(SuperSplatError):
    """The plugin or upload request is not configured correctly."""


class ProtocolError(SuperSplatError):
    """The service returned an invalid or inconsistent response."""


class UploadCancelled(SuperSplatError):
    """The current export or upload was cancelled by the user."""


class ApiError(SuperSplatError):
    """An HTTP API request failed."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
        url: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.status is None or self.status in {408, 425, 429} or (
            self.status is not None and 500 <= self.status <= 599
        )


class PartUploadError(ApiError):
    """A signed multipart PUT failed."""
