from aeam.agents.rag.retrieval_pipeline import RetrievalPipeline
from aeam.integrations.embedding_service import EmbeddingService
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

def test_queries_low_threshold():
    embed_service = EmbeddingService()
    qdrant = QdrantClient(host="localhost", port=6333)
    # We'll create a pipeline with a low threshold to see scores
    pipeline = RetrievalPipeline(
        embedding_service=embed_service,
        qdrant_client=qdrant,
        collection="aeam_documents",
        similarity_threshold=0.001  # Set low to get all results
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
            # We need to call the internal search method to bypass the threshold? Actually, we can just use the pipeline with threshold 0.0
            results = pipeline.search(query=query, top_k=10)
            print(f"  Found {len(results)} results (threshold=0.0)")
            if results:
                for i, r in enumerate(results[:5]):
                    print(f"    {i+1}. similarity={r['similarity']:.4f}, chunk_id={r['chunk_id'][:8]}..., text={r['text'][:100]}...")
            else:
                print("    No results")
        except Exception as e:
            print(f"  Error: {e}")

if __name__ == "__main__":
    test_queries_low_threshold()