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
            text: Input string to encode. If empty or whitespace-only, returns
                  an empty list.

        Returns:
            A ``list[float]`` of length 384, or an empty list if input is empty.

        Example::

            vec = service.encode_text("Memory leak in payment service")
            assert len(vec) == 384
            empty = service.encode_text("")
            assert empty == []
        """
        if not text or not text.strip():
            return []

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
        batches. Empty or whitespace-only strings are silently skipped; the
        output list contains vectors only for the non‑empty inputs, preserving
        order relative to the original list (empty entries produce no vector).

        Args:
            texts: List of input strings. May be empty or contain empty strings.

        Returns:
            A ``list[list[float]]`` of vectors for the non‑empty inputs. If the
            input list is empty or all strings are empty, returns an empty list.

        Example::

            vecs = service.encode_batch([
                "CPU spike detected",
                "",
                "Disk I/O saturated",
            ])
            # vecs contains two vectors (for first and last items)
            assert len(vecs) == 2
            assert len(vecs[0]) == 384
        """
        if not texts:
            return []

        clean_texts = [t for t in texts if t and t.strip()]
        if not clean_texts:
            return []

        embeddings = self._model.encode(
            clean_texts,
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