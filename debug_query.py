#!/usr/bin/env python3

import sys
from datetime import datetime, timezone
sys.path.insert(0, 'D:\\AEAM_COPY')

from aeam.agents.rag.rag_agent import RAGAgent
from aeam.core.event_models import Event

# Create a dummy event similar to the test
event = Event(
    event_id="test",
    event_type="KPI_ANOMALY",
    metric="cpu",
    severity="HIGH",
    current_value=90.0,
    expected_value=50.0,
    detection_methods=["zscore"],
    metadata={},
    timestamp=datetime.now(timezone.utc)
)

# We need to create a RAGAgent to access _formulate_query, but it's static
# So we can call it directly
from aeam.agents.rag.rag_agent import RAGAgent

query = RAGAgent._formulate_query(event)
print(f"Current query for KPI_ANOMALY with metric 'cpu': '{query}'")

# Test another one
event2 = Event(
    event_id="test2",
    event_type="DB_LATENCY",
    metric="db_query_time",
    severity="HIGH",
    current_value=100.0,
    expected_value=50.0,
    detection_methods=["threshold"],
    metadata={"service": "payment-service", "host": "db-server-01"},
    timestamp=datetime.now(timezone.utc)
)

query2 = RAGAgent._formulate_query(event2)
print(f"Current query for DB_LATENCY with metric 'db_query_time' and metadata: '{query2}'")

# Test one more
event3 = Event(
    event_id="test3",
    event_type="SALES_DROP",
    metric="daily_sales",
    severity="CRITICAL",
    current_value=1000.0,
    expected_value=5000.0,
    detection_methods=["zscore", "threshold"],
    metadata={"service": "ecommerce", "region": "us-east"},
    timestamp=datetime.now(timezone.utc)
)

query3 = RAGAgent._formulate_query(event3)
print(f"Current query for SALES_DROP with metric 'daily_sales' and metadata: '{query3}'")