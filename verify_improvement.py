#!/usr/bin/env python3

import sys
from datetime import datetime, timezone
sys.path.insert(0, 'D:\\AEAM_COPY')

from aeam.agents.rag.rag_agent import RAGAgent as CurrentRAGAgent
from aeam.core.event_models import Event
from aeam.integrations.embedding_service import EmbeddingService
from qdrant_client import QdrantClient
from aeam.agents.rag.retrieval_pipeline import RetrievalPipeline

# ----- OLD _formulate_query (current implementation) -----
# We will use the CurrentRAGAgent to get old queries.

# ----- NEW _formulate_query (proposed improvement) -----
# We will define a new version that uses more concise event descriptions
# based on the startup runbook.

def _get_concise_event_description(event_type: str) -> str:
    """
    Map event type to a concise phrase based on the startup runbook.
    We read the runbook and extracted the most common phrases.
    """
    # These were extracted from aeam/knowledge/startup_runbook.md
    mapping = {
        "DB_LATENCY": "database latency",
        "SALES_DROP": "sales drop",
        "SALES_SPIKE": "sales spike",
        "KPI_ANOMALY": "KPI anomaly",
        "CPU_HIGH": "CPU high",
        "MEMORY_HIGH": "memory high",
        "DISK_IO": "disk IO high",
        "NETWORK_ERROR": "network error",
        "ERROR_RATE": "error rate high",
        "LATENCY_HIGH": "latency high",
        "CACHE_MISS": "cache miss",
        "QUEUE_BACKLOG": "queue backlog",
        "DEPLOYMENT_FAILURE": "deployment failure",
        "AUTH_FAILURE": "authentication failure",
    }
    return mapping.get(event_type, event_type.replace("_", " ").lower())

def _new_formulate_query(event: Event) -> str:
    """
    New query formulation: concise event description + normalized metric + metadata fragments.
    """
    event_desc = _get_concise_event_description(event.event_type)
    metric_nl = CurrentRAGAgent._normalise_query_fragment(event.metric)
    parts = [event_desc]
    if metric_nl:
        parts.append(metric_nl)
    parts.extend(CurrentRAGAgent._metadata_query_fragments(event.metadata or {}))
    return " ".join(parts)

# ----- Test events for the four types -----
def make_event(event_type: str, metric: str, metadata: dict) -> Event:
    return Event(
        event_id=f"test-{event_type}",
        event_type=event_type,
        metric=metric,
        severity="HIGH",
        current_value=90.0,
        expected_value=50.0,
        detection_methods=["zscore"],
        metadata=metadata,
        timestamp=datetime.now(timezone.utc),
    )

events = [
    make_event(
        "DB_LATENCY",
        "db_query_time",
        {"service": "payment-service", "host": "db-server-01"},
    ),
    make_event(
        "SALES_DROP",
        "daily_sales",
        {"service": "ecommerce", "region": "us-east"},
    ),
    make_event(
        "CPU_HIGH",
        "cpu_utilization",
        {"service": "web-app", "host": "web-server-01"},
    ),
    make_event(
        "KPI_ANOMALY",
        "cpu",
        {},  # no metadata for KPI_ANOMALY in our test
    ),
]

# ----- Print old and new queries -----
print("=== OLD vs NEW QUERIES ===")
for ev in events:
    old_query = CurrentRAGAgent._formulate_query(ev)
    new_query = _new_formulate_query(ev)
    print(f"\nEvent: {ev.event_type} | Metric: {ev.metric}")
    print(f"  OLD: {old_query}")
    print(f"  NEW: {new_query}")

# ----- Setup retrieval pipeline -----
print("\n=== SETTING UP RETRIEVAL PIPELINE ===")
try:
    embed_service = EmbeddingService()
    qdrant = QdrantClient(host="localhost", port=6333)
    pipeline = RetrievalPipeline(
        embedding_service=embed_service,
        qdrant_client=qdrant,
        collection="aeam_documents",
        similarity_threshold=0.0001,  # Very low to get all results
        default_top_k=10,
    )
    print("Retrieval pipeline ready.")
except Exception as e:
    print(f"Failed to setup retrieval pipeline: {e}")
    sys.exit(1)

# ----- Function to get top similarities for a query -----
def get_top_similarities(query: str, limit: int = 10):
    try:
        results = pipeline.search(query=query, top_k=limit)
        similarities = [r["similarity"] for r in results]
        return similarities
    except Exception as e:
        print(f"Error searching for query '{query}': {e}")
        return []

# ----- Evaluate old and new queries -----
print("\n=== RETRIEVAL RESULTS (Top-10 Similarities) ===")
print(f"{'Event':<15} {'Type':<8} {'Top-1 Similarity':<18} {'Top-5 Avg':<12} {'Query'}")
print("-" * 100)
for ev in events:
    old_query = CurrentRAGAgent._formulate_query(ev)
    new_query = _new_formulate_query(ev)

    old_sims = get_top_similarities(old_query, limit=10)
    new_sims = get_top_similarities(new_query, limit=10)

    # Pad lists to length 10 for averaging
    old_sims_padded = old_sims + [0.0] * (10 - len(old_sims))
    new_sims_padded = new_sims + [0.0] * (10 - len(new_sims))

    old_top1 = old_sims[0] if old_sims else 0.0
    new_top1 = new_sims[0] if new_sims else 0.0
    old_top5_avg = sum(old_sims_padded[:5]) / 5.0
    new_top5_avg = sum(new_sims_padded[:5]) / 5.0

    print(f"{ev.event_type:<15} {'OLD':<8} {old_top1:<18.4f} {old_top5_avg:<12.4f} {old_query}")
    print(f"{'':<15} {'NEW':<8} {new_top1:<18.4f} {new_top5_avg:<12.4f} {new_query}")
    print()

# ----- Improvement summary -----
print("\n=== IMPROVEMENT SUMMARY ===")
print("Comparing Top-1 similarity scores:")
for ev in events:
    old_query = CurrentRAGAgent._formulate_query(ev)
    new_query = _new_formulate_query(ev)
    old_sims = get_top_similarities(old_query, limit=1)
    new_sims = get_top_similarities(new_query, limit=1)
    old_top1 = old_sims[0] if old_sims else 0.0
    new_top1 = new_sims[0] if new_sims else 0.0
    improvement = new_top1 - old_top1
    print(f"{ev.event_type}: {old_top1:.4f} -> {new_top1:.4f} ({improvement:+.4f})")

# ----- Check if new queries meet the threshold (0.7) -----
print("\n=== THRESHOLD CHECK (0.7) ===")
print("Event           | Type | Max Similarity | Meets Threshold?")
print("-" * 50)
for ev in events:
    new_query = _new_formulate_query(ev)
    # Get all results (with threshold 0.0001) and see if any >= 0.7
    results = pipeline.search(query=new_query, top_k=20)  # get more to be sure
    max_sim = max((r["similarity"] for r in results), default=0.0)
    meets = max_sim >= 0.7
    print(f"{ev.event_type:<15} | {'NEW':<4} | {max_sim:<14.4f} | {'YES' if meets else 'NO'}")

print("\nNote: The retrieval pipeline uses a similarity threshold of 0.7 (Phase 4 spec).")
print("A query is considered effective if it returns at least one result with similarity >= 0.7.")