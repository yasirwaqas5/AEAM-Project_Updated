from aeam.agents.rag.retrieval_pipeline import RetrievalPipeline
from aeam.integrations.embedding_service import EmbeddingService
from qdrant_client import QdrantClient

def test_queries():
    embed_service = EmbeddingService()
    qdrant = QdrantClient(host="localhost", port=6333)
    pipeline = RetrievalPipeline(
        embedding_service=embed_service,
        qdrant_client=qdrant,
        collection="aeam_documents"
    )

    queries = [
        "Database latency",
        "Sales drop",
        "Checkout failure",
        "Payment gateway failure",
        "API latency",
        "Memory exhaustion",
        "CPU saturation",
        "Disk I/O",
        "Queue backlog",
        "Worker saturation"
    ]

    for query in queries:
        print(f"\nQuery: {query}")
        try:
            results = pipeline.search(query=query, top_k=5)
            print(f"  Found {len(results)} results")
            if results:
                for i, r in enumerate(results):
                    print(f"    {i+1}. similarity={r['similarity']:.4f}, chunk_id={r['chunk_id'][:8]}..., text={r['text'][:100]}...")
            else:
                print("    No results")
        except Exception as e:
            print(f"  Error: {e}")

if __name__ == "__main__":
    test_queries()