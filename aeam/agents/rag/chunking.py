"""
aeam/agents/rag/chunking.py

Document chunking strategies for the AEAM RAG pipeline.

Pure text preprocessing module. Contains zero imports from embedding,
Qdrant, LLM, or any AEAM application module. It accepts raw text and
returns a list of chunk dicts — nothing more.

Three strategies are supported:
- ``"fixed"``     — split on character count with configurable overlap.
- ``"sentence"``  — split on sentence boundaries (``"."``, ``"!"``, ``"?"``).
- ``"paragraph"`` — split on double newlines (``"\\n\\n"``).

Chunk IDs are derived deterministically from the source document's metadata
and the chunk's position, so re-chunking identical text always produces the
same IDs.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


class TextChunker:
    """
    Splits plain text into overlapping or non-overlapping chunks.

    Each produced chunk carries a deterministic ``chunk_id``, the chunk text,
    and a copy of the caller-supplied metadata enriched with chunk-level
    positional fields (``chunk_index``, ``chunk_total``).

    Args:
        chunk_size: Target character length per chunk.
                    - ``"fixed"``     — hard split boundary.
                    - ``"sentence"``  — soft limit; a new chunk begins after
                      the sentence that pushes the running length over this
                      value.
                    - ``"paragraph"`` — soft limit; paragraphs that exceed
                      this value are included as a single oversized chunk
                      rather than split mid-paragraph.
                    Defaults to ``512``.
        overlap:    Number of characters from the end of the previous chunk to
                    prepend to the next chunk. Provides context continuity.
                    Ignored by the ``"paragraph"`` strategy.
                    Must be strictly less than ``chunk_size``.
                    Defaults to ``50``.
        strategy:   Chunking strategy. One of ``"fixed"``, ``"sentence"``,
                    ``"paragraph"``. Defaults to ``"sentence"``.

    Raises:
        ValueError: If ``chunk_size`` < 1, ``overlap`` < 0,
                    ``overlap`` >= ``chunk_size``, or ``strategy`` is unknown.

    Example::

        chunker = TextChunker(chunk_size=512, overlap=50, strategy="sentence")
        chunks = chunker.chunk_text(
            text="The CPU spiked. Memory held steady. Disk I/O was normal.",
            metadata={"source": "incident_report", "incident_id": "INC-42"},
        )
        for c in chunks:
            print(c["chunk_id"], c["text"])
    """

    SUPPORTED_STRATEGIES: frozenset[str] = frozenset({"fixed", "sentence", "paragraph"})

    def __init__(
        self,
        chunk_size: int = 512,
        overlap: int = 50,
        strategy: str = "sentence",
    ) -> None:
        """
        Initialise the TextChunker with chunking parameters.

        Args:
            chunk_size: Target character length per chunk. Must be >= 1.
            overlap:    Overlap character count. Must be >= 0 and < chunk_size.
            strategy:   One of ``"fixed"``, ``"sentence"``, ``"paragraph"``.

        Raises:
            ValueError: On invalid parameter values.
        """
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1. Got: {chunk_size}.")
        if overlap < 0:
            raise ValueError(f"overlap must be >= 0. Got: {overlap}.")
        if overlap >= chunk_size:
            raise ValueError(
                f"overlap ({overlap}) must be strictly less than "
                f"chunk_size ({chunk_size})."
            )
        if strategy not in self.SUPPORTED_STRATEGIES:
            raise ValueError(
                f"Unknown strategy {strategy!r}. "
                f"Must be one of: {sorted(self.SUPPORTED_STRATEGIES)}."
            )

        self._chunk_size = chunk_size
        self._overlap = overlap
        self._strategy = strategy

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def chunk_size(self) -> int:
        """Target character length per chunk."""
        return self._chunk_size

    @property
    def overlap(self) -> int:
        """Character overlap between consecutive chunks."""
        return self._overlap

    @property
    def strategy(self) -> str:
        """Active chunking strategy name."""
        return self._strategy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk_text(
        self,
        text: str,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Split ``text`` into chunks according to the configured strategy.

        Each chunk dict contains:
        - ``chunk_id``  — deterministic SHA-256-based identifier.
        - ``text``      — the chunk's text content (stripped).
        - ``metadata``  — caller-supplied metadata merged with chunk-level
          positional fields (``chunk_index``, ``chunk_total``, ``strategy``).

        Empty or whitespace-only ``text`` returns an empty list.
        ``metadata`` is never mutated; each chunk receives its own shallow copy.

        Args:
            text:     The source text to chunk. May contain any Unicode.
            metadata: Arbitrary key-value pairs to attach to every chunk
                      (e.g. ``{"incident_id": "INC-42", "source": "report"}``).
                      Must not contain ``chunk_index``, ``chunk_total``, or
                      ``strategy`` — those are reserved for chunk-level fields.

        Returns:
            Ordered list of chunk dicts. Empty list if ``text`` is blank.

        Raises:
            ValueError: If ``metadata`` uses any reserved key
                        (``chunk_index``, ``chunk_total``, ``strategy``).

        Example::

            chunks = chunker.chunk_text(
                text="First sentence. Second sentence. Third sentence.",
                metadata={"doc_id": "d-001"},
            )
            # [
            #   {"chunk_id": "...", "text": "First sentence. Second sentence.",
            #    "metadata": {"doc_id": "d-001", "chunk_index": 0, ...}},
            #   ...
            # ]
        """
        _RESERVED = {"chunk_index", "chunk_total", "strategy"}
        conflicts = _RESERVED & set(metadata.keys())
        if conflicts:
            raise ValueError(
                f"metadata contains reserved keys: {sorted(conflicts)}. "
                f"These are set automatically per chunk."
            )

        if not text or not text.strip():
            return []

        # Dispatch to the appropriate strategy.
        if self._strategy == "fixed":
            raw_chunks = self._split_fixed(text)
        elif self._strategy == "sentence":
            raw_chunks = self._split_sentence(text)
        else:  # "paragraph"
            raw_chunks = self._split_paragraph(text)

        # Filter out blank chunks produced by splitting.
        raw_chunks = [c.strip() for c in raw_chunks if c.strip()]

        total = len(raw_chunks)
        results: list[dict[str, Any]] = []

        for idx, chunk_text in enumerate(raw_chunks):
            chunk_meta = {
                **metadata,
                "chunk_index": idx,
                "chunk_total": total,
                "strategy": self._strategy,
            }
            chunk_id = self._make_chunk_id(metadata=metadata, index=idx, text=chunk_text)
            results.append({
                "chunk_id": chunk_id,
                "text": chunk_text,
                "metadata": chunk_meta,
            })

        return results

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _split_fixed(self, text: str) -> list[str]:
        """
        Split ``text`` into fixed-length character slices with overlap.

        Each slice is exactly ``chunk_size`` characters (or shorter for the
        final slice). The next slice starts at ``chunk_size - overlap``
        characters after the previous one.

        Args:
            text: Source text.

        Returns:
            List of raw text slices (may include whitespace-only entries
            which are filtered by the caller).
        """
        step = self._chunk_size - self._overlap
        chunks: list[str] = []
        start = 0

        while start < len(text):
            end = start + self._chunk_size
            chunks.append(text[start:end])
            start += step

        return chunks

    def _split_sentence(self, text: str) -> list[str]:
        """
        Split ``text`` on sentence boundaries with soft chunk_size limit.

        Sentences are detected by a terminal punctuation mark (``.``, ``!``,
        ``?``) optionally followed by closing quotes or brackets, then
        whitespace. A new chunk begins after the sentence that causes the
        running character count to exceed ``chunk_size``.

        Overlap is implemented by prepending the tail of the previous chunk
        (up to ``overlap`` characters, trimmed to the nearest sentence
        boundary) to the next chunk.

        Args:
            text: Source text.

        Returns:
            List of chunk strings.
        """
        # Split into sentences while preserving delimiters.
        # Fixed-width lookbehind – works in all Python versions.
        sentence_pattern = re.compile(r'(?<=[.!?])\s+')
        sentences = sentence_pattern.split(text.strip())
        if not sentences:
            return [text]

        chunks: list[str] = []
        current_parts: list[str] = []
        current_len: int = 0
        overlap_tail: str = ""

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            # If adding this sentence breaches chunk_size AND we already have
            # content, flush the current chunk first.
            if current_len + len(sentence) > self._chunk_size and current_parts:
                chunk_text = " ".join(current_parts)
                chunks.append(overlap_tail + chunk_text if overlap_tail else chunk_text)

                # Compute overlap tail from the end of the flushed chunk.
                overlap_tail = self._compute_overlap_tail(chunk_text)
                current_parts = []
                current_len = 0

            current_parts.append(sentence)
            current_len += len(sentence) + 1  # +1 for the space between sentences

        # Flush remaining sentences.
        if current_parts:
            chunk_text = " ".join(current_parts)
            chunks.append(overlap_tail + chunk_text if overlap_tail else chunk_text)

        return chunks

    def _split_paragraph(self, text: str) -> list[str]:
        """
        Split ``text`` on double newlines (paragraph boundaries).

        Paragraphs that exceed ``chunk_size`` are kept as a single oversized
        chunk rather than split mid-paragraph. Overlap is not applied (paragraph
        boundaries are natural context breaks).

        Args:
            text: Source text.

        Returns:
            List of paragraph strings.
        """
        paragraphs = re.split(r'\n\s*\n', text.strip())
        chunks: list[str] = []
        current_parts: list[str] = []
        current_len: int = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if current_len + len(para) > self._chunk_size and current_parts:
                chunks.append("\n\n".join(current_parts))
                current_parts = []
                current_len = 0

            current_parts.append(para)
            current_len += len(para)

        if current_parts:
            chunks.append("\n\n".join(current_parts))

        return chunks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_overlap_tail(self, text: str) -> str:
        """
        Extract the trailing ``overlap`` characters from ``text``.

        Trims to the nearest word boundary to avoid splitting mid-word.
        Returns an empty string if ``overlap`` is zero.

        Args:
            text: The chunk text to extract a tail from.

        Returns:
            Trailing overlap string (may be empty).
        """
        if self._overlap == 0 or not text:
            return ""

        tail = text[-self._overlap:]

        # Trim to the nearest word boundary to avoid mid-word splits.
        space_idx = tail.find(" ")
        if space_idx > 0:
            tail = tail[space_idx + 1:]

        return tail + " " if tail else ""

    @staticmethod
    def _make_chunk_id(
        metadata: dict[str, Any],
        index: int,
        text: str,
    ) -> str:
        """
        Generate a deterministic chunk ID using SHA-256.

        The ID is derived from a stable composite of:
        - Any ``"incident_id"``, ``"doc_id"``, or ``"source"`` key in metadata
          (in that priority order), or all metadata values joined if none match.
        - The chunk index.
        - The first 64 characters of the chunk text (for uniqueness within
          a document when metadata alone is not unique).

        This ensures that re-chunking identical text with identical metadata
        always produces the same IDs.

        Args:
            metadata: Caller-supplied metadata dict.
            index:    Zero-based chunk position.
            text:     Chunk text content.

        Returns:
            Lowercase hexadecimal SHA-256 digest string (64 characters).
        """
        # Prefer a stable document identifier from metadata.
        doc_key = (
            metadata.get("incident_id")
            or metadata.get("doc_id")
            or metadata.get("source")
            or "|".join(str(v) for v in sorted(metadata.values(), key=str))
        )

        raw = f"{doc_key}::{index}::{text[:64]}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            f"TextChunker("
            f"strategy={self._strategy!r}, "
            f"chunk_size={self._chunk_size}, "
            f"overlap={self._overlap})"
        )