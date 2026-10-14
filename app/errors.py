"""One error type for every business rule. The `code` strings are the API contract."""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")
