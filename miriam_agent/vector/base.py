"""Vector store base class for Miriam Financial Agent."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class VectorStore(ABC):
    """Abstract base class for vector storage backends."""

    @abstractmethod
    async def store_embedding(
        self,
        id: str,
        content: str,
        embedding: List[float],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Store a vector embedding with its content."""
        ...

    @abstractmethod
    async def search_similar(
        self,
        query_embedding: List[float],
        limit: int = 10,
        threshold: float = 0.7,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for similar vectors using cosine similarity."""
        ...

    @abstractmethod
    async def delete_embedding(self, id: str) -> bool:
        """Delete a vector embedding by ID."""
        ...

    @abstractmethod
    async def update_embedding(
        self,
        id: str,
        embedding: List[float],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update an existing vector embedding."""
        ...
