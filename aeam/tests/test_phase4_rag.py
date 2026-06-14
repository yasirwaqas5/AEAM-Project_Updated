import pytest
from unittest.mock import MagicMock

from aeam.integrations.embedding_service import EmbeddingService
from aeam.agents.rag.chunking import TextChunker
from aeam.agents.rag.response_validator import RAGResponseValidator
from aeam.agents.rag.rag_agent import RAGAgent


# ============================================================
# 1️⃣ EmbeddingService
# ============================================================

def test_embedding_dimension():
    service = EmbeddingService()
    assert service.dimension == 384


def test_embedding_empty_string():
    service = EmbeddingService()
    result = service.encode_text("")
    assert isinstance(result, list)
    assert result == []


def test_embedding_batch():
    service = EmbeddingService()
    texts = ["hello world", "aeam test"]
    result = service.encode_batch(texts)
    assert isinstance(result, list)
    assert len(result) == 2


# ============================================================
# 2️⃣ TextChunker
# ============================================================

@pytest.mark.parametrize("strategy", ["fixed", "sentence", "paragraph"])
def test_chunking_strategies(strategy):
    chunker = TextChunker(strategy=strategy)
    text = "First sentence. Second sentence.\n\nNew paragraph."
    metadata = {"source": "test_doc"}

    chunks = chunker.chunk_text(text, metadata)

    assert isinstance(chunks, list)
    assert len(chunks) >= 1

    for chunk in chunks:
        assert "chunk_id" in chunk
        assert "text" in chunk
        assert "metadata" in chunk
        assert chunk["metadata"]["source"] == "test_doc"


def test_chunk_id_deterministic():
    chunker = TextChunker(strategy="fixed")
    text = "Deterministic test"
    metadata = {}

    chunks1 = chunker.chunk_text(text, metadata)
    chunks2 = chunker.chunk_text(text, metadata)

    assert chunks1[0]["chunk_id"] == chunks2[0]["chunk_id"]


# ============================================================
# 3️⃣ RAGResponseValidator
# ============================================================

def test_validator_valid_case():
    validator = RAGResponseValidator()

    retrieved = [{"chunk_id": "abc"}]

    output = {
        "possible_causes": [
            {"cause": "CPU spike", "chunk_id": "abc", "confidence": 0.8}
        ],
        "overall_confidence": 0.8,
        "requires_human_review": False,
    }

    valid, _ = validator.validate(output, retrieved)
    assert valid is True


def test_validator_invalid_chunk():
    validator = RAGResponseValidator()

    retrieved = [{"chunk_id": "abc"}]

    output = {
        "possible_causes": [
            {"cause": "CPU spike", "chunk_id": "xyz", "confidence": 0.8}
        ],
        "overall_confidence": 0.8,
        "requires_human_review": False,
    }

    valid, _ = validator.validate(output, retrieved)
    assert valid is False


def test_validator_invalid_confidence():
    validator = RAGResponseValidator()

    retrieved = [{"chunk_id": "abc"}]

    output = {
        "possible_causes": [
            {"cause": "CPU spike", "chunk_id": "abc", "confidence": 2.0}
        ],
        "overall_confidence": 1.5,
        "requires_human_review": False,
    }

    valid, _ = validator.validate(output, retrieved)
    assert valid is False


# ============================================================
# 4️⃣ RAGAgent
# ============================================================

class DummyEvent:
    event_id = "1"
    event_type = "KPI_ANOMALY"
    metric = "cpu"
    severity = "HIGH"
    current_value = 90
    expected_value = 50
    detection_methods = ["zscore"]
    metadata = {}


def test_rag_no_chunks():
    retrieval = MagicMock()
    retrieval.search.return_value = []

    llm = MagicMock()
    validator = MagicMock()

    agent = RAGAgent(retrieval, validator, llm)

    result = agent.investigate(DummyEvent(), MagicMock())

    assert result["confidence"] == 0.0
    assert result["findings"]["retrieved_count"] == 0


def test_rag_llm_bad_json():
    retrieval = MagicMock()
    retrieval.search.return_value = [{"chunk_id": "abc", "text": "x", "metadata": {}, "similarity": 0.9}]

    llm = MagicMock()
    llm.query.return_value = "not json"

    validator = MagicMock()

    agent = RAGAgent(retrieval, validator, llm)

    result = agent.investigate(DummyEvent(), MagicMock())

    assert result["confidence"] == 0.0
    assert "error" in result["findings"]


def test_rag_valid_flow():
    retrieval = MagicMock()
    retrieval.search.return_value = [{"chunk_id": "abc", "text": "x", "metadata": {}, "similarity": 0.9}]

    llm = MagicMock()
    llm.query.return_value = """
    {
        "possible_causes": [
            {"cause": "CPU spike", "chunk_id": "abc", "confidence": 0.8}
        ],
        "overall_confidence": 0.8,
        "requires_human_review": false
    }
    """

    validator = MagicMock()
    validator.validate.return_value = (True, "valid")

    agent = RAGAgent(retrieval, validator, llm)

    result = agent.investigate(DummyEvent(), MagicMock())

    assert result["confidence"] == 0.8
    assert result["findings"]["validation_passed"] is True