"""Read-only Go document-result client (Stage 2).

Thin wrapper over the shared ``httpx`` client inside ``GoBackendClient``:
same base URL, same user JWT, same CSRF header, same timeout. Parses the
``{"data": {...}}`` envelope into ``DocumentResult``; contract violations
become ``ValidationError``.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError as PydanticValidationError

from miriam_agent.core.exceptions import IntegrationError, ValidationError
from miriam_agent.documents.errors import map_http_status, map_transport_error
from miriam_agent.documents.schemas import DocumentResult


async def fetch_document_result(
    http_client: httpx.AsyncClient, token: str, document_id: str
) -> DocumentResult:
    try:
        resp = await http_client.get(
            f"/api/v1/documents/{document_id}/result",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as e:
        raise map_transport_error(e) from e
    if resp.status_code != 200:
        raise map_http_status(resp.status_code, resp.text)
    try:
        payload: dict[str, Any] = resp.json()
    except ValueError as e:
        raise IntegrationError(f"Go document result not JSON: {e}") from e
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise IntegrationError("Go document result missing data envelope")
    try:
        return DocumentResult.model_validate(data)
    except PydanticValidationError as e:
        raise ValidationError(f"invalid document contract: {e}") from e
