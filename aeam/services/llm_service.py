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
    incident_cost_scope,
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
        # Hardening: one reused provider client per service instance instead
        # of a fresh one on every retry attempt (see generate()).
        self._client = None

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
        # Hardening: the real provider error used to be swallowed by
        # `logger.warning(...)` and the final raise said only "Failed to
        # generate LLM response after retries". That string is what got
        # persisted into every failed incident's findings, so an operator
        # reading the record could not tell an expired API key from a
        # decommissioned model from a rate limit from a network timeout — the
        # diagnosis existed only in a transient WARNING log line. The last
        # error is now carried into the raised message and therefore into the
        # persisted evidence.
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                logger.info(f"Generating LLM response (attempt {attempt + 1})")
                if provider == "groq":
                    # Hardening: the client is built ONCE per call rather than
                    # once per retry attempt. Three attempts meant three
                    # clients and three httpx connection pools per call, and
                    # with five RAG passes per investigation that was up to
                    # fifteen pools created (and never explicitly closed) for
                    # a single incident.
                    client = self._groq_client()
                    t = start_timer()
                    chat = client.chat.completions.create(
                        messages=[{"role": "user", "content": prompt}],
                        model=self._model_id(),
                        temperature=kwargs.get("temperature", 0.2),
                        max_tokens=kwargs.get("max_tokens", 1000),
                    )
                    end_timer(llm_call_duration_seconds.labels(provider=provider), t)
                    llm_calls_total.labels(provider=provider, status="success").inc()
                    self._record_usage_metrics(provider, chat)
                    # Hardening: a success must clear the failure tally.
                    # Previously _failure_count only ever grew, so a service
                    # with two historical failures tripped its breaker on the
                    # next one no matter how many thousands of calls had
                    # succeeded in between.
                    self._failure_count = 0
                    return chat.choices[0].message.content
                else:
                    raise LLMServiceException(f"Unsupported provider: {provider}")
            except Exception as e:
                last_error = e
                logger.warning(
                    "LLM call failed (attempt %d/%d) | %s: %s",
                    attempt + 1, max_retries, type(e).__name__, e,
                )
                # Hardening: do not burn retries on an error that cannot
                # succeed on retry. An invalid key (401), a revoked key (403),
                # or a decommissioned model (404) is permanent — retrying it
                # three times added 1+2+4s of pure sleep per call, which across
                # five RAG passes was ~35s of dead latency added to every
                # investigation before it could report the failure.
                if self._is_permanent_error(e):
                    logger.error(
                        "LLM call failed permanently (not retryable) | %s: %s",
                        type(e).__name__, e,
                    )
                    break
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        await self._record_failure()
        llm_calls_total.labels(provider=provider, status="failure").inc()
        raise LLMServiceException(
            f"Failed to generate LLM response after retries: "
            f"{type(last_error).__name__}: {last_error}"
            if last_error is not None
            else "Failed to generate LLM response after retries"
        )

    # ------------------------------------------------------------------
    # Provider plumbing
    # ------------------------------------------------------------------

    def _model_id(self) -> str:
        """The chat model id for the configured provider.

        Kept as one named accessor so the id is not buried mid-call. The
        value is unchanged from before this hardening pass (COMPAT-1); it is
        overridable via ``LLM_MODEL`` for an operator whose provider account
        has a different model available, because a hardcoded id silently
        becomes a permanent 404 the day a vendor decommissions it.
        """
        configured = str(getattr(self.settings, "LLM_MODEL", "") or "").strip()
        return configured or "llama-3.1-8b-instant"

    def _groq_client(self):
        """One lazily-created, reused Groq client (see the retry-loop note)."""
        import groq

        if self._client is None:
            self._client = groq.Groq(
                api_key=self.settings.LLM_API_KEY,
                # Phase E8 (AI-3): per-call timeout, bounding every one of the
                # shared call sites uniformly.
                timeout=self.settings.LLM_TIMEOUT_SECONDS,
            )
        return self._client

    @staticmethod
    def _is_permanent_error(exc: Exception) -> bool:
        """True for provider errors that retrying cannot fix.

        Detected by HTTP status where the SDK exposes one, so this stays
        correct without importing provider-specific exception classes.
        """
        status = getattr(exc, "status_code", None)
        if status is None:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
        if isinstance(status, int) and status in {400, 401, 403, 404, 422}:
            return True
        return isinstance(exc, LLMServiceException)

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

            # Phase E11: attribute this call's usage to the investigation that
            # caused it, when one is in flight on this thread. A no-op outside
            # an investigation (background/manual calls stay counted only by
            # the global E8 counters above — inventing an incident attribution
            # for them would be dishonest).
            incident_cost_scope.record_llm(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LLMService | usage metering skipped: %s", exc)
