"""
aeam/services/llm_service.py

The one shared LLM client every call site in AEAM uses (RAGAgent,
QueryExpansionAgent, PolicyExtractor, DecisionEngine, Orchestrator's
depth>=3 LLM reasoning, ReportAgent) — see docs/ai_governance.md for the
full call-site register.

Phase E8 (AI Governance) hardening, applied here so every call site
inherits it automatically without per-site changes:

- **Provider truth (AI-1, SEC-4 pattern applied to AI config).** The
  supported-provider set below is exactly what is implemented. Configuring
  anything else while a real call would actually be attempted
  (``LLM_ENABLED=true`` and ``USE_MOCK_LLM=false``) aborts construction
  with an explicit message — never a vendor the platform cannot reach,
  silently promised.
- **Per-call timeout (AI-3).** ``Settings.LLM_TIMEOUT_SECONDS`` is passed
  to the provider client, bounding every call.
- **Metering (AI-6).** Every call — mock or real — increments
  ``llm_calls_total``. Real calls additionally observe
  ``llm_call_duration_seconds`` and, when the provider reports usage,
  ``llm_tokens_total`` / ``llm_cost_usd_total`` (the latter using the
  operator-configured, honestly-zero-by-default per-1k-token rates).
"""

import asyncio
import logging

from aeam.config.settings import Settings
from aeam.monitoring.metrics import (
    end_timer,
    llm_call_duration_seconds,
    llm_calls_total,
    llm_cost_usd_total,
    llm_tokens_total,
    start_timer,
)

logger = logging.getLogger(__name__)


class LLMServiceException(Exception):
    pass


# Phase E8 (AI-1): the supported-provider list equals the implemented
# list. This is the single source of truth — Settings.LLM_PROVIDER's
# default and docs/ai_governance.md's provider support statement both
# describe this set, they do not duplicate it.
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"groq"})


class LLMService:
    def __init__(self, settings: Settings, secret_manager=None):
        self.settings = settings
        self.secret_manager = secret_manager
        self.use_mock = getattr(settings, 'USE_MOCK_LLM', True)
        self._failure_count = 0
        self._circuit_open = False
        self._last_failure_time = 0
        self._circuit_timeout = 60

        # Phase E8 (AI-1, SEC-4 pattern): fail loudly at construction time
        # if a real call would actually be attempted against an
        # unimplemented provider. A mock-mode or LLM-disabled posture never
        # reaches a provider, so it is never blocked by this check —
        # exactly like E3's JWT key check only fires when a real
        # verification would occur.
        if self.settings.LLM_ENABLED and not self.use_mock:
            provider = (self.settings.LLM_PROVIDER or "").strip().lower()
            if provider not in _SUPPORTED_PROVIDERS:
                raise LLMServiceException(
                    f"Unsupported LLM_PROVIDER={provider!r}. Implemented "
                    f"providers: {sorted(_SUPPORTED_PROVIDERS)}. Set "
                    f"LLM_PROVIDER to a supported value, or set "
                    f"USE_MOCK_LLM=true / LLM_ENABLED=false to run without "
                    f"a real provider. Startup aborted (Phase E8, AI-1)."
                )

    async def _check_circuit(self):
        if self._circuit_open:
            import time
            if time.time() - self._last_failure_time > self._circuit_timeout:
                self._circuit_open = False
                self._failure_count = 0
            else:
                raise LLMServiceException("LLM Circuit Breaker is OPEN")

    async def _record_failure(self):
        import time
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= 3:
            self._circuit_open = True

    def query(self, prompt, *, temperature=0.7, max_tokens=1000):
        try:
            loop = asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self.generate(prompt, temperature=temperature, max_tokens=max_tokens),
                )
                return future.result()
        except RuntimeError:
            return asyncio.run(self.generate(prompt, temperature=temperature, max_tokens=max_tokens))

    async def generate(self, prompt: str, **kwargs) -> str:
        if self.use_mock or not self.settings.LLM_ENABLED:
            llm_calls_total.labels(provider="mock", status="mock").inc()
            return "This is a mock LLM response based on the spec."

        await self._check_circuit()

        provider = self.settings.LLM_PROVIDER.lower()
        # Phase E8 (AI-1): defense-in-depth — the constructor already
        # refused to build a service configured this way, but Settings is
        # not enforced-immutable, so this stays as a second, cheap check
        # rather than trusting construction-time state forever.
        if provider not in _SUPPORTED_PROVIDERS:
            raise LLMServiceException(
                f"Unsupported provider: {provider}. Implemented: "
                f"{sorted(_SUPPORTED_PROVIDERS)}."
            )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"Generating LLM response (attempt {attempt + 1})")
                if provider == "groq":
                    import groq
                    # Phase E8 (AI-3): per-call timeout, bounding every one
                    # of the six shared call sites uniformly.
                    client = groq.Groq(
                        api_key=self.settings.LLM_API_KEY,
                        timeout=self.settings.LLM_TIMEOUT_SECONDS,
                    )
                    t = start_timer()
                    chat = client.chat.completions.create(
                        messages=[{"role": "user", "content": prompt}],
                        model="llama-3.1-8b-instant",
                        temperature=kwargs.get("temperature", 0.2),
                        max_tokens=kwargs.get("max_tokens", 1000),
                    )
                    end_timer(llm_call_duration_seconds.labels(provider=provider), t)
                    llm_calls_total.labels(provider=provider, status="success").inc()
                    self._record_usage_metrics(provider, chat)
                    return chat.choices[0].message.content
                else:
                    raise LLMServiceException(f"Unsupported provider: {provider}")
            except Exception as e:
                logger.warning(f"LLM call failed: {e}")
                await asyncio.sleep(2 ** attempt)

        await self._record_failure()
        llm_calls_total.labels(provider=provider, status="failure").inc()
        raise LLMServiceException("Failed to generate LLM response after retries")

    def _record_usage_metrics(self, provider: str, chat) -> None:
        """
        Best-effort token/cost metering (Phase E8, AI-6).

        Reads ``chat.usage`` if the provider response includes it. Never
        raises — a missing/unexpected usage shape simply means this call's
        tokens are not counted, never a fabricated estimate.
        """
        try:
            usage = getattr(chat, "usage", None)
            if usage is None:
                return
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

            if prompt_tokens:
                llm_tokens_total.labels(provider=provider, kind="prompt").inc(prompt_tokens)
            if completion_tokens:
                llm_tokens_total.labels(provider=provider, kind="completion").inc(completion_tokens)

            prompt_rate = float(getattr(self.settings, "LLM_COST_PER_1K_PROMPT_TOKENS_USD", 0.0) or 0.0)
            completion_rate = float(getattr(self.settings, "LLM_COST_PER_1K_COMPLETION_TOKENS_USD", 0.0) or 0.0)
            cost = (prompt_tokens / 1000.0) * prompt_rate + (completion_tokens / 1000.0) * completion_rate
            if cost:
                llm_cost_usd_total.labels(provider=provider).inc(cost)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLMService | usage metering skipped: %s", exc)
