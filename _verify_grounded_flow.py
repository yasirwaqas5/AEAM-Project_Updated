"""Verification: RAG → Orchestrator → STM → LTM → Dashboard flow without depth-3 LLM synthesis."""
import json
import os
from datetime import datetime, timezone

from aeam.config.settings import Settings
from aeam.core.event_models import Event
from aeam.memory.short_term import ShortTermMemory
from aeam.memory.long_term import LongTermMemory
from aeam.integrations.database import DatabaseClient
from aeam.agents.orchestrator.decision_engine import DecisionEngine
from aeam.agents.orchestrator.evaluation_engine import EvaluationEngine
from aeam.agents.orchestrator.state_machine import IncidentStateMachine
from aeam.agents.orchestrator.orchestrator import Orchestrator
from aeam.agents.rag.rag_agent import RAGAgent
from aeam.agents.rag.response_validator import RAGResponseValidator


class MockRetrieval:
    """Returns realistic chunks from startup_runbook.md."""
    def search(self, query, top_k=5):
        return [
            {
                "chunk_id": "runbook_sales_001",
                "text": (
                    "Sales Anomaly Investigation: Symptoms include sudden drop in sales revenue. "
                    "Likely causes: Checkout failure, payment gateway issues, pricing errors. "
                    "Investigation: Compare current vs expected sales, verify payment gateway health."
                ),
                "similarity": 0.85,
                "metadata": {"source": "startup_runbook.md"},
            },
            {
                "chunk_id": "runbook_checkout_002",
                "text": (
                    "Checkout Failure Investigation: Symptoms include increase in abandoned carts. "
                    "Likely causes: Payment gateway downtime, coupon bugs, cart session corruption."
                ),
                "similarity": 0.79,
                "metadata": {"source": "startup_runbook.md"},
            },
        ]


class MockLLM:
    """Returns grounded JSON citing actual retrieved chunk_ids."""
    def query(self, prompt, *, temperature, max_tokens):
        return json.dumps({
            "possible_causes": [
                {
                    "cause": "Payment gateway downtime preventing transaction completion",
                    "chunk_id": "runbook_sales_001",
                    "confidence": 0.87,
                },
                {
                    "cause": "Checkout service experiencing session corruption",
                    "chunk_id": "runbook_checkout_002",
                    "confidence": 0.82,
                },
            ],
            "overall_confidence": 0.85,
            "requires_human_review": False,
        })


class NoOpVector:
    def upsert(self, collection, payload):
        pass


def main():
    SEP = "=" * 72
    db_path = os.path.join(os.getcwd(), "_verify_flow_tmp.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    # Force LLM_ENABLED=False to isolate the RAG contract fix path
    os.environ["LLM_ENABLED"] = "false"
    settings = Settings()
    print(SEP)
    print(f"Settings: LLM_ENABLED={settings.LLM_ENABLED} (depth-3 synthesis OFF)")
    print(SEP)

    db = DatabaseClient(database_url=f"sqlite:///{db_path}")
    ltm = LongTermMemory(database_client=db, vector_client=NoOpVector())
    stm = ShortTermMemory()

    rag = RAGAgent(
        retrieval_pipeline=MockRetrieval(),
        validator=RAGResponseValidator(),
        llm_service=MockLLM(),
        top_k=5,
    )

    orch = Orchestrator(
        event_bus=object(),
        decision_engine=DecisionEngine(settings=settings),
        evaluation_engine=EvaluationEngine(settings=settings),
        short_term_memory=stm,
        long_term_memory=ltm,
        state_machine=IncidentStateMachine(),
        settings=settings,
        rag_agent=rag,
    )

    event = Event(
        event_id="verify-001",
        event_type="SALES_DROP",
        metric="sales_amount",
        current_value=800.0,
        expected_value=5000.0,
        detection_methods=["zscore", "prophet"],
        severity="HIGH",
        timestamp=datetime.now(tz=timezone.utc),
        metadata={"region": "US-WEST"},
    )

    print("Triggering HIGH severity incident (depth-3 LLM synthesis OFF)...")
    orch.handle_event(event)

    # Query the database for the persisted incident
    row = db.fetch_one("SELECT * FROM incidents ORDER BY timestamp DESC LIMIT 1")

    print(SEP)
    print("DATABASE row (source of truth for dashboard):")
    print(SEP)
    if row:
        print(f"  incident_id        : {row['incident_id']}")
        print(f"  event_type         : {row['event_type']}")
        print(f"  metric             : {row['metric']}")
        print(f"  severity           : {row['severity']}")
        print(f"  investigation_depth: {row['investigation_depth']}")
        print(f"  root_cause         : {row['root_cause']!r}")
        print(f"  confidence         : {row['confidence']}")
        print(f"  llm_response       : {row['llm_response']!r}")
        print(SEP)
        
        # Check if root_cause contains grounded reasoning
        rc = row["root_cause"] or ""
        is_placeholder = "placeholder" in rc.lower() or "Simulated root cause" in rc
        is_grounded = any(
            keyword in rc.lower()
            for keyword in ["payment", "gateway", "checkout", "session", "corruption"]
        )
        
        print("VERIFICATION RESULT:")
        print(SEP)
        print(f"  ✓ Root cause is KPI placeholder     : {is_placeholder}")
        print(f"  ✓ Root cause is grounded RAG output : {is_grounded}")
        print(f"  ✓ LLM response persisted (RAG JSON) : {bool(row['llm_response'])}")
        print(SEP)
        
        if is_grounded and not is_placeholder:
            print("✅ SUCCESS: Grounded RAG reasoning reached the database")
            print("   without relying on depth-3 LLM synthesis!")
        else:
            print("❌ FAILURE: KPI placeholder still present")
    else:
        print("❌ No incident persisted")

    db.dispose()
    if os.path.exists(db_path):
        os.remove(db_path)


if __name__ == "__main__":
    main()
