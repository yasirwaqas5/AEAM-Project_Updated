# AEAM AI Governance (Phase E8)

Provider support statement and the AI call-site register (AI-6). This
document is the E8 deliverable referenced by `ROADMAP.md`'s
"Documentation updates" line for that phase.

---

## 1. Provider support statement

`aeam/services/llm_service.py` is the ONE shared LLM client every call
site in AEAM uses. Its supported-provider set is a single source of
truth (`_SUPPORTED_PROVIDERS`):

| Provider | Status |
|---|---|
| `groq` | **Implemented.** The only provider `LLMService.generate()` can actually call. |
| anything else (e.g. `gemini`, `openai`) | **Not implemented.** Configuring it while a real call would be attempted aborts startup. |

### The provider-truth contract (AI-1)

`LLMService.__init__` fails loudly — the SEC-4 fail-closed pattern
applied to AI configuration — when **all** of the following hold:

1. `Settings.LLM_ENABLED = true`
2. `Settings.USE_MOCK_LLM = false`
3. `Settings.LLM_PROVIDER` is not in the implemented set.

Any posture that would never reach a real provider (mock mode, or
`LLM_ENABLED=false`) is never blocked by this check — it only fires when
a real call would actually be attempted. `Settings.LLM_PROVIDER`'s
default is `"groq"` (changed from the pre-E8 `"gemini"`, which was never
implemented) so the out-of-the-box configuration is coherent.

### `USE_MOCK_LLM` environment posture

| Environment | Recommended `USE_MOCK_LLM` | Why |
|---|---|---|
| Local dev (`docker-compose.yml`) | `true` (default) | Free, deterministic, no API key required. |
| CI / automated tests | `true` (Settings default; tests never set `LLM_ENABLED=true`) | No test depends on a real model; `FakeLLMService`/mocked fixtures are used throughout. |
| Staging | `false` (with a real `groq` key) | Staging should exercise the real integration path before it reaches production — mock-by-default masking integration issues was itself an audited finding. |
| Production (`deploy/cloudrun.yaml`) | `true` while `LLM_ENABLED=false` (current posture); flip to `false` together with `LLM_ENABLED=true` when the product turns real LLM reasoning on in production | Explicit, not implicit — `deploy/cloudrun.yaml` declares both values so there is never an accidental "prod is secretly mocked" or "prod is secretly real" state. |

Every real (non-mock) call increments the `llm_calls_total{status="success"|"failure"}`
Prometheus counter under the real provider's label; every mocked call
increments `llm_calls_total{provider="mock", status="mock"}`. An
operator can therefore always answer "is this environment actually
calling a real model?" from `/metrics` alone, without reading code.

---

## 2. Guardrail wiring (AI-1)

`aeam/security/llm_guardrails.py` defines two pure functions —
`sanitize_input` (strips known prompt-injection patterns) and
`validate_output` (rejects text matching sensitive-data patterns).
Before Phase E8 both were fully implemented and tested but **wired to
nothing** (a standing AI-7 violation). E8 wires them at every point
untrusted content enters a prompt, and before every point LLM-generated
text is persisted or displayed:

| Call site | `sanitize_input` applied to | `validate_output` applied to |
|---|---|---|
| `RAGAgent._assemble_prompt` (`aeam/agents/rag/rag_agent.py`) | Retrieved chunk text (`chunk.get("text")`) — ingested-document content, the primary untrusted-input surface for this site. | The raw LLM response, before JSON parsing / persistence into `findings` / display in the Evidence panel. |
| `QueryExpansionAgent.expand` (`aeam/agents/rag/query_expansion.py`) | The (deterministically-formulated, but metadata-influenced) original query, before it is embedded in the expansion prompt. | The raw LLM response, before parsing the candidate query variants (which are later displayed in the Retrieval Summary / `query_attempts`). |
| `PolicyExtractor.extract` (`aeam/intelligence/policy_extraction.py`) | The full extracted document text — the canonical injection surface (an uploaded file's contents), sanitized **before** truncation to `_MAX_PROMPT_CHARS` so an injection phrase cannot dodge detection by falling on the truncation boundary. | The raw LLM response, before parsing the policy list persisted into the `policies` table and displayed in the Knowledge Center UI. |

A `validate_output` rejection is treated exactly like any other recoverable
failure mode already established at that call site (unparsable JSON,
LLM exception): logged, and the caller degrades gracefully — `RAGAgent`
returns a structured `_error_result`, `QueryExpansionAgent` falls back to
`[original_query]`, `PolicyExtractor` returns `[]`. Nothing is ever
silently passed through, and nothing is ever fabricated in its place.

**Not wired in E8** (outside the roadmap's named scope for this phase):
`DecisionEngine._build_prompt`, the Orchestrator's depth≥3 LLM reasoning
prompt, and `ReportAgent`. Their prompt inputs are lower-risk (event
fields, already-computed STM/findings state) and they were not named in
`ROADMAP.md`'s "Files expected to change" for E8. They still inherit the
E8 timeout/metering hardening below automatically, since all six share
one `LLMService` instance.

---

## 3. AI call-site register (AI-6)

Every LLM call in AEAM goes through the shared `LLMService`, so timeout
and metering are declared **once**, here, rather than per-site. What
differs per site is prompt content, temperature, max_tokens, and failure
mode.

| # | Call site | File | Temperature | Max tokens | Guardrails | Failure mode |
|---|---|---|---|---|---|---|
| 1 | RAG investigation reasoning | `aeam/agents/rag/rag_agent.py::RAGAgent.investigate` | 0.2 | 1000 | sanitize_input (chunks) + validate_output (response) | Never raises — returns a structured `_error_result` with an `"error"` field; Orchestrator's investigation loop proceeds without a root cause. |
| 2 | Multi-query expansion | `aeam/agents/rag/query_expansion.py::QueryExpansionAgent.expand` | 0.3 | 300 | sanitize_input (query) + validate_output (response) | Never raises — degrades to `[original_query]`, i.e. single-query retrieval. |
| 3 | Policy extraction | `aeam/intelligence/policy_extraction.py::PolicyExtractor.extract` | 0.0 | 1500 | sanitize_input (document text) + validate_output (response) | Never raises — returns `[]` (no policies extracted this pass); ingestion (indexing) already succeeded independently and is never blocked by this. |
| 4 | Hybrid rule/LLM decision | `aeam/agents/orchestrator/decision_engine.py::DecisionEngine.decide` | 0.2 | 1000 | Not wired (see §2) | On LLM/parse failure, `DecisionEngine` falls back to the deterministic rule-based decision — LLM reasoning is advisory to the hybrid decision, never the sole path. |
| 5 | Deep investigation reasoning (depth ≥ 3) | `aeam/agents/orchestrator/orchestrator.py::Orchestrator._investigate` | 0.2 | 500 | Not wired (see §2) | On failure, the existing KPI placeholder root cause (already computed earlier in the same pass) is left intact — never overwritten with a fabricated value. |
| 6 | Investigation report / alert generation | `aeam/agents/report/report_agent.py::ReportAgent` | 0.4 | 1200 | Not wired (see §2) | `ReportAgent` has a fully deterministic fallback mode (template-based, no LLM) — an LLM failure degrades to that, never a missing report. |

**Shared, service-level (applies to all six):**

- **Timeout (AI-3):** `Settings.LLM_TIMEOUT_SECONDS` (default 30s), passed
  to the provider client at construction.
- **Retry:** up to 3 attempts with exponential backoff (`2**attempt`
  seconds), pre-existing behavior unchanged by E8.
- **Circuit breaker:** pre-existing — opens after 3 consecutive failures
  across the whole service, closes after 60s.
- **Metering (AI-6):** `llm_calls_total{provider,status}` on every call
  (mock or real); `llm_call_duration_seconds{provider}` on real calls;
  `llm_tokens_total{provider,kind}` and `llm_cost_usd_total{provider}`
  when the provider response includes usage data. All published through
  the existing `aeam/monitoring/metrics.py` Prometheus module — no
  second metrics pipeline (OBS-1/OBS-2).
- **Budget:** no hard spend cap yet — `llm_cost_usd_total` is
  informational (OBS-2 semantics: honestly zero until an operator
  configures `LLM_COST_PER_1K_{PROMPT,COMPLETION}_TOKENS_USD`). The
  ROADMAP's own "Future extensibility" note flags enforceable budgets as
  a later addition on top of this same metering, not a redesign.

---

## 4. Regression guarantee (AI-2)

Wiring guardrails changes *rejection* behavior only for text that
actually matches an injection or sensitive-data pattern — the full RAG
regression ledger (grounding, validation, hybrid/multi-query/rerank
fusion) is unaffected for ordinary content, and is asserted unchanged by
this phase's test suite (`aeam/tests/test_phase_e8_ai_governance.py`)
alongside the pre-existing phase-4/7/9/C1/C6 RAG suites, all of which
still pass unmodified.
