"""Document intelligence contract v1 (Stage 2).

Read-only consumption of Go-owned document results. Go owns auth,
storage, lifecycle, and persistence; Python validates the versioned
envelope and surfaces it to Miriam. No DB access, no file handling here.
"""

from miriam_agent.documents.schemas import (
    SUPPORTED_MAJOR_VERSION,
    DocumentData,
    DocumentResult,
)

__all__ = ["SUPPORTED_MAJOR_VERSION", "DocumentData", "DocumentResult"]
