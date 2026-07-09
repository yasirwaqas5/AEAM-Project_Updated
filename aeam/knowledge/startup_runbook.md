# AEAM SRE Runbook

## Sales Anomaly Investigation
**Symptoms:** Sudden drop or spike in sales revenue; deviation from expected value beyond threshold.
**Likely causes:** Checkout failure, payment gateway issues, pricing errors, traffic acquisition problems, tracking/data pipeline faults.
**Evidence:** Payment error rates, checkout conversion, cart abandonment, recent deployments, campaign status, analytics freshness.
**Metrics:** sales_amount, conversion_rate, checkout_failure_rate, payment_gateway_error_rate.
**Logs:** payment service logs, checkout service logs, web server access logs, analytics pipeline logs.
**Investigation:** Compare current vs expected sales, check traffic sources, verify payment gateway health, inspect recent code changes.
**Escalation:** If anomaly persists >15 min or customer impact >5% of revenue.
**Resolution:** Fix checkout/payment bug, rollback deployment, correct pricing, restore data pipeline.

## Checkout Failure Investigation
**Symptoms:** Increase in abandoned carts, decrease in completed orders, rise in checkout error responses.
**Likely causes:** Payment gateway downtime, coupon/promo code bugs, shipping calculation errors, UI glitches, cart session corruption.
**Evidence:** Checkout error logs, payment gateway API responses, cart abandonment metrics, recent promo changes.
**Metrics:** checkout_success_rate, cart_abandonment_rate, avg_checkout_time, payment_decline_rate.
**Logs:** checkout microservice logs, payment gateway logs, frontend error logs, session store logs.
**Investigation:** Verify payment gateway health, test coupon codes, inspect recent frontend changes, review cart session handling.
**Escalation:** If checkout success rate drops below 90% for >5 min.
**Resolution:** Restore payment gateway, fix coupon logic, rollback UI changes, clear corrupted sessions.

## Payment Gateway Failure Investigation
**Symptoms:** Spike in payment errors, timeouts, or declined transactions; customers unable to complete purchase.
**Likely causes:** Gateway API downtime, credential expiration, network connectivity issues, fraud detection false positives.
**Evidence:** Gateway error responses, latency metrics, credential validity, network traceroutes.
**Metrics:** payment_error_rate, avg_payment_latency, timeout_rate, decline_rate.
**Logs:** payment service logs, gateway API logs, network logs, fraud service logs.
**Investigation:** Check gateway status page, validate API keys, test connectivity, review fraud rule triggers.
**Escalation:** If payment_error_rate >5% for >2 min.
**Resolution:** Switch to backup gateway, renew credentials, fix network, adjust fraud thresholds.

## API Latency Investigation
**Symptoms:** Increased response times for API endpoints; higher p95/p99 latency; timeouts.
**Likely causes:** Backend service slowdown, database overload, external dependency latency, resource saturation (CPU/memory).
**Evidence:** Endpoint latency metrics, dependency call traces, resource utilization, recent load spikes.
**Metrics:** api_latency_p95, api_latency_p99, error_rate, request_per_second.
**Logs:** API gateway logs, service logs, database slow query logs, external service logs.
**Investigation:** Identify slow endpoints, trace downstream dependencies, check DB query performance, monitor host resources.
**Escalation:** If latency >2x baseline for >5 min or error rate >1%.
**Resolution:** Scale backend, optimize slow queries, increase timeouts, retry failed dependencies.

## Database Latency Investigation
**Symptoms:** Slow query responses, increased commit latency, application timeouts.
**Likely causes:** Missing indexes, lock contention, inefficient queries, insufficient IOPS, replication lag.
**Evidence:** Slow query log, lock wait metrics, execution plans, replication lag metrics.
**Metrics:** query_latency_avg, query_latency_p95, lock_wait_time, replication_lag_seconds.
**Logs:** database logs (Postgres: pg_log), pg_stat_statements, CloudWatch/Azure metrics.
**Investigation:** Enable slow_query_log, review top queries, check index usage, examine lock tables, verify replica sync.
**Escalation:** If latency >5s for >2 min or replication lag >30s.
**Resolution:** Add missing indexes, kill long-running locks, rewrite inefficient queries, scale storage/storage IOPS.

## PostgreSQL Bottleneck Investigation
**Symptoms:** High CPU usage on DB node, rising connection count, query queuing.
**Likely causes:** Insufficient connections, inefficient sequential scans, autovacuum overload, temp table bloat.
**Evidence:** pg_stat_activity, pg_stat_user_tables, autovacuum logs, CPU/memory metrics.
**Metrics:** db_cpu_usage, active_connections, waiting_connections, seq_scan_ratio.
**Logs:** PostgreSQL log, pg_stat_activity, autovacuum logs.
**Investigation:** Check for idle-in-transaction, run `EXPLAIN ANALYZE` on slow queries, monitor autovacuum, review connection pool settings.
**Escalation:** If CPU >80% for >5 min or waiting connections >50% of max.
**Resolution:** Increase max_connections, add indexes, tune autovacuum, terminate idle connections, vacuum analyze.

## Deadlocks Investigation
**Symptoms:** Spike in transaction aborts, deadlock errors in logs, application errors about lock wait timeout.
**Likely causes:** Circular lock dependencies, long-running transactions, missing row-level indexes, high concurrency on same rows.
**Evidence:** Deadlock details in log, pg_locks, transaction IDs, affected tables.
**Metrics:** deadlock_count, lock_timeout_errors, rollback_rate.
**Logs:** PostgreSQL log (look for `deadlock detected`), application error logs.
**Investigation:** Extract deadlock details from logs, examine query patterns, add indexes to reduce lock scope, shorten transactions.
**Escalation:** If deadlock_count >10 per minute.
**Resolution:** Apply index fixes, refactor transactions to acquire locks in consistent order, reduce lock duration.

## Connection Pool Exhaustion Investigation
**Symptoms:** Connection acquisition timeouts, increased latency, errors like "too many connections".
**Likely causes:** Pool size too low, connection leaks, long-running queries, burst traffic.
**Evidence:** Pool checkout/wait metrics, active connection count, recent deployments, leak detection.
**Metrics:** pool_wait_time, pool_usage_percent, connection_leak_count.
**Logs:** Application pool logs, DB connection logs.
**Investigation:** Check pool configuration, look for unclosed connections, identify long queries, review recent traffic spikes.
**Escalation:** If pool wait time >1s for >2 min.
**Resolution:** Increase pool size, fix connection leaks, kill long queries, scale application instances.

## Cache Failure Investigation
**Symptoms:** Increased backend load, higher latency, cache miss rate spike, occasional stale data.
**Likely causes:** Redis downtime, memory eviction, network partition, incorrect TTL, cache stampede.
**Evidence:** Redis INFO output, miss/hit ratios, latency, eviction keys, network logs.
**Metrics:** cache_hit_ratio, eviction_count, latency_increase, memory_used_percent.
**Logs:** Redis logs, application cache logs, network logs.
**Investigation:** PING Redis, check memory fragmentation, review key TTLs, inspect network connectivity, assess stampede protection.
**Escalation:** If cache hit ratio drops below 80% for >3 min.
**Resolution:** Restart Redis, increase memory, adjust TTL, implement cache warming, fix network.

## Redis Issues Investigation
**Symptoms:** High latency, timeouts, memory errors, failed commands.
**Likely causes:** Insufficient memory, slow commands (KEYS *), persistence issues, network latency, CPU saturation.
**Evidence:** Redis latency metrics, slowlog, memory usage, CPU usage, network round-trip.
**Metrics:** avg_latency, p99_latency, used_memory_percent, rejected_connections.
**Logs:** Redis log, slowlog, application logs.
**Investigation:** Run `SLOWLOG GET`, check `INFO memory`, review persistence AOF/RDB, test network ping, monitor CPU.
**Escalation:** If latency >100ms for >2 min or memory >90%.
**Resolution:** Increase memory, delete slow keys, tune persistence, scale Redis cluster, fix network.

## CPU Saturation Investigation
**Symptoms:** Host CPU usage near 100%, increased response times, queue backlog.
**Likely causes:** Runaway processes, inefficient code, insufficient instances, traffic spike, background jobs.
**Evidence:** Top processes, per‑core utilization, recent autoscaling events, deployment history.
**Metrics:** cpu_utilization_percent, load_avg, run_queue_length.
**Logs:** system logs (journald), container logs, application logs.
**Investigation:** Identify top CPU consumers, check for infinite loops, review recent code, verify autoscaling rules.
**Escalation:** If CPU >95% for >3 min.
**Resolution:** Kill rogue processes, scale out, optimize hot code paths, adjust autoscaling thresholds.

## Memory Exhaustion Investigation
**Symptoms:** OOM kills, increased swap usage, application crashes, GC overhead.
**Likely causes:** Memory leaks, large object retention, insufficient heap, caching too much data.
**Evidence:** Heap dump, GC logs, swap usage, object counts, recent memory‑intensive changes.
**Metrics:** memory_used_percent, swap_used, oom_kill_count, gc_pause_time.
**Logs:** kernel logs, application GC logs, heap dumps.
**Investigation:** Analyze heap for leaks, check for large collections, review cache sizes, force GC.
**Escalation:** If OOM kills >0 in 5 min or swap usage >50%.
**Resolution:** Fix memory leaks, increase heap/container limit, clear caches, restart services.

## Disk I/O Investigation
**Symptoms:** High disk latency, slowed read/write, application timeouts on file operations.
**Likely causes:** Disk saturation, failing hardware, inefficient I/O patterns, noisy neighbors.
**Evidence:** iostat metrics, await/util %, SMART errors, recent large file transfers.
**Metrics:** disk_util_percent, avg_request_latency, iops, await_time.
**Logs:** dmesg, syslog, container logs, storage provider alerts.
**Investigation:** Check iostat for high await, review SMART, look for bursty workloads, verify RAID/replication health.
**Escalation:** If disk util >80% for >5 min or await >20ms.
**Resolution:** Replace failing disk, redistribute load, optimize I/O patterns, increase storage performance.

## Queue Backlog Investigation
**Symptoms:** Growing depth in message queues, increased processing latency, consumer lag.
**Likely causes:** Consumer slowdown, producer burst, network partition, insufficient workers.
**Evidence:** Queue depth metrics, consumer lag, processing rates, recent autoscaling events.
**Metrics:** queue_depth, consumer_lag, messages_per_sec, processing_time_avg.
**Logs:** queue broker logs (RabbitMQ/Kafka), consumer logs, producer logs.
**Investigation:** Check consumer health, pause producers if needed, scale consumers, inspect network.
**Escalation:** If queue depth >2x baseline for >5 min.
**Resolution:** Scale out consumers, throttle producers, fix network, restart stuck consumers.

## Worker Saturation Investigation
**Symptoms:** Worker CPU/memory high, task processing delays, increased retry rates.
**Likely causes:** Long-running tasks, insufficient worker pool, task queue congestion, external dependency latency.
**Evidence:** Worker utilization, task latency, retry counts, pool size, external API metrics.
**Metrics:** worker_cpu_percent, worker_memory_percent, task_latency_avg, retry_rate.
**Logs:** worker logs, task queue logs, external service logs.
**Investigation:** Profile long tasks, increase worker count, optimize task logic, check external dependencies.
**Escalation:** If worker saturation >90% for >3 min.
**Resolution:** Add workers, split long tasks, tune pool size, mitigate external latency.

## Deployment Failure Investigation
**Symptoms:** Deployment health checks failing, rollback triggered, increased error rates post‑deploy.
**Likely causes:** Bad container image, config drift, database migration errors, insufficient readiness probes.
**Evidence:** Deployment logs, rollback events, pre/post‑deploy metrics, image scan results.
**Metrics:** deploy_success_rate, rollback_count, post_deploy_error_rate.
**Logs:** CI/CD pipeline logs, Kubernetes events, application logs.
**Investigation:** Review deploy logs, verify image integrity, check config maps, test migration scripts, probe endpoints.
**Escalation:** If deploy fails for >2 consecutive attempts.
**Resolution:** Rollback, fix image/config, rerun migration, adjust readiness probes, redeploy.

## Rollback Procedure Investigation
**Symptoms:** Need to revert to previous stable state after failed deployment or incident.
**Likely causes:** Deployment introduced breaking bug, data corruption, config error.
**Evidence:** Current vs previous version tags, rollback logs, data integrity checks.
**Metrics:** rollback_time, data_loss_flag, post_rollback_error_rate.
**Logs:** rollback orchestration logs, application logs, DB change logs.
**Investigation:** Confirm rollback target, verify data backups, ensure traffic drained, monitor post‑rollback health.
**Escalation:** If rollback takes >10 min or health checks fail after rollback.
**Resolution:** Execute rollback plan, validate system, re‑route traffic, perform smoke test.

## Kubernetes CrashLoopBackOff Investigation
**Symptoms:** Pods repeatedly crashing, restarting, never reaching Ready state.
**Likely causes:** Application errors, missing config/secrets, insufficient resources, liveness probe too aggressive.
**Evidence:** Pod events, container logs, crash reason, exit codes, resource limits.
**Metrics:** restart_count, crash_loop_duration, ready_state_time.
**Logs:** kubectl describe pod, container logs, kubelet logs.
**Investigation:** Examine container stderr/stdout, check ConfigMaps/Secrets, review liveness/readiness probes, adjust resources.
**Escalation:** If pod remains in CrashLoopBackOff >5 min.
**Resolution:** Fix application bug, supply missing config, adjust probes, increase resource limits, redeploy.

## Autoscaling Issues Investigation
**Symptoms:** Instances not scaling despite metric thresholds, or scaling too aggressively causing churn.
**Likely causes:** Misconfigured alarms, stale metric windows, cooldown too high/low, metric latency.
**Evidence:** Autoscaling group activity, CloudWatch alarm states, metric timestamps, scaling policies.
**Metrics:** desired_capacity, actual_capacity, scaling_activity_count.
**Logs:** autoscaling logs, CloudWatch alarm history, metric streams.
**Investigation:** Verify alarm thresholds, check metric delivery timeliness, review cooldowns, inspect recent policy changes.
**Escalation:** If scaling activity >10 events in 5 min with no improvement.
**Resolution:** Adjust alarm thresholds, tune cooldowns, fix metric pipeline, replace faulty metric.

## Network Failure Investigation
**Symptoms:** Increased latency, packet loss, timeouts, intermittent connectivity.
**Likely causes:** Link flapping, misconfigured routing, overload, hardware failure, DNS issues.
**Evidence:** ping/traceroute results, interface error counters, congestion monitoring, recent config changes.
**Metrics:** latency_ms, packet_loss_percent, jitter, retransmission_rate.
**Logs:** router/switch logs, firewall logs, host network logs.
**Investigation:** Perform ping/traceroute, check interface errors, review BGP/OSPF, inspect recent ACL changes.
**Escalation:** If packet loss >2% for >2 min or latency >100ms baseline.
**Resolution:** Replace faulty cable, correct routing, increase bandwidth, mitigate congestion.

## DNS Failure Investigation
**Symptoms:** Name resolution failures, increased latency, SERVFAIL/NXDOMAIN spikes.
**Likely causes:** DNS server downtime, misconfigured records, cache poisoning, network partition, TTL too low.
**Evidence:** dig/nslookup results, query logs, server status, recent zone changes.
**Metrics:** resolution_latency, failed_query_rate, cache_hit_ratio.
**Logs:** DNS server logs, resolver logs, network logs.
**Investigation:** Query authoritative servers, check zone serials, clear caches, test connectivity to DNS.
**Escalation:** If resolution fails >5% of requests for >3 min.
**Resolution:** Restart DNS service, fix zone records, increase TTL, repair network.

## SSL Failure Investigation
**Symptoms:** TLS handshake failures, certificate errors, mixed content warnings.
**Likely causes:** Expired/invalid cert, misconfigured SNI, protocol mismatch, intermediate missing.
**Evidence:** openssl s_client output, cert expiry dates, SNI settings, protocol version logs.
**Metrics:** handshake_failure_rate, cert_validity_days, protocol_error_rate.
**Logs:** web server logs (nginx/apache), load balancer logs, client error logs.
**Investigation:** Run s_client, verify cert chain, confirm SNI, check protocol support, review recent cert changes.
**Escalation:** If handshake failures >1% for >2 min.
**Resolution:** Renew cert, fix SNI config, update protocols, install missing intermediates.

## Authentication Issues Investigation
**Symptoms:** Login failures, token rejection, increased unauthorized errors.
**Likely causes:** Identity provider downtime, secret/key mismatch, clock skew, policy changes, brute force lockouts.
**Evidence:** auth logs, token validation errors, IDP status, recent policy changes, failed attempt counts.
**Metrics:** login_failure_rate, token_validation_error, lockout_count.
**Logs:** auth service logs, IDP logs, application logs, security audit logs.
**Investigation:** Check IDP health, validate secrets/keys, verify NTP sync, review IAM policies, inspect lockout thresholds.
**Escalation:** If login failure rate >5% for >3 min.
**Resolution:** Restore IDP, rotate keys, sync clocks, adjust policies, clear lockouts.

## Monitoring Investigation Investigation
**Symptoms:** Missing alerts, delayed notifications, duplicate alerts, silent failures.
**Likely causes:** Misconfigured alert rules, notification channel overload, silences, metric collection gaps.
**Evidence:** Alertmanager status, notification logs, rule evaluation logs, metric drop metrics.
**Metrics:** alert_latency, notification_delivery_rate, duplicate_alert_rate.
**Logs:** Alertmanager logs, notification channel logs, Prometheus server logs.
**Investigation:** Review alert routing tree, check notification quotas, verify rule timestamps, inspect scrape targets.
**Escalation:** If alert delivery delay >2 min for critical alerts.
**Resolution:** Fix alert rules, increase notification capacity, remove stale silences, restore scrape targets.

## Incident Escalation Investigation
**Symptoms:** Incident not progressing, SLA breach, stakeholder concern, lack of ownership.
**Likely causes:** Missing escalation policy, unresponsive owner, inadequate communication, severity mis‑classified.
**Evidence:** escalation logs, ownership timestamps, communication logs, prior similar incidents.
**Metrics:** time_to_acknowledge, time_to_escalate, stakeholder_satisfaction.
**Logs:** incident ticket logs, chat logs, email logs.
**Investigation:** Verify escalation policy, contact primary owner, update stakeholders, reassess severity.
**Escalation:** If SLA breach imminent (>50% elapsed) or no owner response.
**Resolution:** Notify secondary owner, invoke war room, adjust severity, document lessons.

## Evidence Collection Investigation
**Symptoms:** Need to gather forensic data post‑incident for analysis or compliance.
**Likely causes:** Lack of predefined collection playbook, insufficient logging, access restrictions.
**Evidence:** Collection scripts output, log archives, snapshots, change logs, access audit.
**Metrics:** collections_completed, evidence_volume, time_to_collect.
**Logs:** collection scripts logs, system audit logs.
**Investigation:** Follow evidence checklist: logs, snapshots, configs, memory dumps, network captures.
**Escalation:** If critical evidence not collected within agreed window.
**Resolution:** Automate collection, increase log retention, grant access, store evidence securely.

## Root Cause Methodology Investigation
**Symptoms:** Repeated incidents, superficial fixes, lack of preventive actions.
**Likely causes:** Poor RCA process, jumping to symptoms, not asking “why” five times, missing data.
**Evidence:** 5‑why diagrams, fishbone charts, corrective actions, recurrence metrics.
**Metrics:** rca_completion_rate, preventive_actions_implemented, incident_recurrence.
**Logs:** RCA meeting logs, action tracking system.
**Investigation:** Assemble timeline, ask why iteratively, validate hypotheses with data, document root cause.
**Escalation:** If RCA not completed within SLA.
**Resolution:** Perform thorough RCA, assign owners for fixes, track effectiveness.

## Jira Evidence Investigation
**Symptoms:** Need to attach relevant runbook/context to Jira tickets for responders.
**Likely causes:** Manual copying, missing automation, unclear what to attach.
**Evidence:** Retrieved chunk IDs, confidence scores, source section, summary sentences.
**Metrics:** tickets_with_evidence, avg_evidence_per_ticket.
**Logs:** ticket update logs, automation runner logs.
**Investigation:** Query retrieval pipeline for top chunks, select those with highest confidence, format as markdown.
**Escalation:** If evidence missing for high‑severity tickets.
**Resolution:** Automate retrieval and comment generation on ticket creation.

## Service Ownership Investigation
**Symptoms:** Unclear who to contact for a service, duplicated effort, orphaned services.
**Likely causes:** Missing ownership metadata, outdated docs, no on‑call rotation.
**Evidence:** OWNERS files, service catalog entries, on‑call schedules, recent ownership changes.
**Metrics:** services_with_owner, ownership_staleness, duplicated_alerts.
**Logs:** service registry logs, documentation update logs.
**Investigation:** Check service catalog, verify OWNERS file, review on‑call schedule, contact tech leads.
**Escalation:** If service lacks owner for >2 incidents.
**Resolution:** Assign owners, update catalog, establish rotation, document runbook links.