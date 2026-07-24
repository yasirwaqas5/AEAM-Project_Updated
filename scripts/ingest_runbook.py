import sys
sys.path.insert(0, '.')
from aeam.integrations.embedding_service import EmbeddingService
from qdrant_client import QdrantClient
from aeam.agents.rag.chunking import TextChunker
from aeam.agents.rag.ingestion_pipeline import IngestionPipeline

def main():
    embed_service = EmbeddingService()
    qdrant = QdrantClient(host="localhost", port=6333)
    # Use chunk size 300, overlap 50 (~16.7% overlap)
    chunker = TextChunker(chunk_size=300, overlap=50, strategy="sentence")
    pipeline = IngestionPipeline(
        embedding_service=embed_service,
        qdrant_client=qdrant,
        chunker=chunker,
        collection="aeam_documents"
    )

    # Read the markdown file
    with open(r"D:\AEAM_COPY\aeam\knowledge\startup_runbook.md", "r", encoding="utf-8") as f:
        text = f.read()

    metadata = {
        "source": "startup_runbook.md",
        "date": "2026-07-04",
        "doc_type": "runbook"
    }

    print("Ingesting document...")
    result = pipeline.ingest_document(text=text, metadata=metadata)
    print("Ingestion result:", result)

    # Get collection info
    try:
        info = qdrant.get_collection(collection_name="aeam_documents")
        print(f"Collection status: {getattr(info, 'status', 'unknown')}")
        print(f"Points count: {getattr(info, 'points_count', 'unknown')}")
        print(f"Vectors count: {getattr(info, 'vectors_count', 'unknown')}")
    except Exception as e:
        print(f"Error getting collection info: {e}")
        # Try alternative method
        try:
            count_result = qdrant.count(collection_name="aeam_documents", exact=True)
            print(f"Points count (via count): {count_result.count}")
        except Exception as e2:
            print(f"Count also failed: {e2}")

if __name__ == "__main__":
    main()