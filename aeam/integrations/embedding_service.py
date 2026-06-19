"""
aeam/integrations/embedding_service.py

Embedding service wrapper for the AEAM modular monolith.

Wraps the ``sentence-transformers`` library to produce 384-dimensional dense
vectors from text using the ``all-MiniLM-L6-v2`` model. This module is
infrastructure-only: it has no knowledge of Qdrant, the Orchestrator, RAGAgent,
or any database logic. It encodes text — nothing more.

The model is loaded once at construction time and reused for all subsequent
calls. No retraining, no fine-tuning, no external API calls.
"""

from __future__ import annotations

import logging

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_MODEL_NAME: str = "all-MiniLM-L6-v2"
_DIMENSION: int = 384


class EmbeddingService:
    """
    Infrastructure wrapper around the ``all-MiniLM-L6-v2`` SentenceTransformer.

    Produces 384-dimensional L2-normalised dense vectors from plain text.
    The model is loaded exactly once at construction and held in memory for
    the lifetime of the instance. No retraining or fine-tuning is performed.

    This class is intentionally isolated from all application logic. It does
    not know about Qdrant, the Orchestrator, RAGAgent, or any database.

    Args:
        model_name: HuggingFace model identifier. Defaults to
                    ``"all-MiniLM-L6-v2"``. Override only in tests.
        device:     Torch device string (e.g. ``"cpu"``, ``"cuda"``).
                    Defaults to ``"cpu"`` for broad compatibility.

    Raises:
        OSError: If the model cannot be loaded (e.g. not yet cached and no
                 network access on first run).

    Example::

        service = EmbeddingService()
        vector = service.encode_text("CPU spike on web-01")
        assert len(vector) == 384
    """

    def __init__(
        self,
        model_name: str = _MODEL_NAME,
        device: str = "cpu",
    ) -> None:
        """
        Load the SentenceTransformer model.

        The model is downloaded to the local HuggingFace cache on first use
        and loaded from disk on subsequent runs.

        Args:
            model_name: Model identifier. Defaults to ``"all-MiniLM-L6-v2"``.
            device:     Inference device. Defaults to ``"cpu"``.

        Raises:
            OSError: If model files cannot be found or downloaded.
        """
        logger.info(
            "EmbeddingService | loading model=%r on device=%r",
            model_name, device,
        )
        self._model: SentenceTransformer = SentenceTransformer(
            model_name_or_path=model_name,
            device=device,
        )
        self._model_name: str = model_name
        self._device: str = device
        logger.info("EmbeddingService | model loaded.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def dimension(self) -> int:
        """Output vector dimensionality (384 for all-MiniLM-L6-v2)."""
        return _DIMENSION

    @property
    def model_name(self) -> str:
        """The model identifier used by this instance."""
        return self._model_name

    def encode_text(self, text: str) -> list[float]:
        """
        Encode a single string into a 384-dimensional embedding vector.

        The output vector is L2-normalised (unit length), which is required
        for cosine similarity to equal the dot product.

        Args:
            text: Input string to encode. Must not be empty or whitespace-only.

        Returns:
            A ``list[float]`` of length 384.

        Raises:
            ValueError: If ``text`` is empty or whitespace-only.

        Example::

            vec = service.encode_text("Memory leak in payment service")
            assert len(vec) == 384
        """
        if not text or not text.strip():
            raise ValueError(
                "encode_text() requires a non-empty, non-whitespace string."
            )

        embedding = self._model.encode(
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return embedding.tolist()

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Encode a list of strings into 384-dimensional embedding vectors.

        Processes all texts in a single forward pass, which is significantly
        more efficient than calling :meth:`encode_text` in a loop for large
        batches. Output order matches input order.

        Args:
            texts: List of input strings. Must not be empty. Each element must
                   be a non-empty, non-whitespace string.

        Returns:
            A ``list[list[float]]`` of length ``len(texts)``, where each inner
            list has length 384.

        Raises:
            ValueError: If ``texts`` is empty, or if any element is empty or
                        whitespace-only. The index of the offending element is
                        included in the error message.

        Example::

            vecs = service.encode_batch([
                "CPU spike detected",
                "Memory usage elevated",
                "Disk I/O saturated",
            ])
            assert len(vecs) == 3
            assert len(vecs[0]) == 384
        """
        if not texts:
            raise ValueError(
                "encode_batch() requires a non-empty list of strings."
            )

        for i, text in enumerate(texts):
            if not text or not text.strip():
                raise ValueError(
                    f"encode_batch() received an empty or whitespace-only string "
                    f"at index {i}."
                )

        embeddings = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            batch_size=32,
            show_progress_bar=False,
        )
        return [e.tolist() for e in embeddings]

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"EmbeddingService("
            f"model={self._model_name!r}, "
            f"dimension={_DIMENSION}, "
            f"device={self._device!r})"
        )