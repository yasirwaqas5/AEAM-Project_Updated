import logging
# groq import moved inside generate method to avoid mandatory dependency
import asyncio
from aeam.config.settings import Settings

logger = logging.getLogger(__name__)

class LLMServiceException(Exception):
    pass

class LLMService:
    def __init__(self, settings: Settings, secret_manager=None):
        self.settings = settings
        self.secret_manager = secret_manager
        self.use_mock = getattr(settings, 'USE_MOCK_LLM', True)
        self._failure_count = 0
        self._circuit_open = False
        self._last_failure_time = 0
        self._circuit_timeout = 60

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
            return "This is a mock LLM response based on the spec."

        await self._check_circuit()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                provider = self.settings.LLM_PROVIDER.lower()
                logger.info(f"Generating LLM response (attempt {attempt + 1})")
                if provider == "groq":
                    import groq
                    client = groq.Groq(api_key=self.settings.LLM_API_KEY)
                    chat = client.chat.completions.create(
                        messages=[{"role": "user", "content": prompt}],
                        model="llama-3.1-8b-instant",
                        temperature=kwargs.get("temperature", 0.2),
                        max_tokens=kwargs.get("max_tokens", 1000),
                    )
                    return chat.choices[0].message.content
                else:
                    raise LLMServiceException(f"Unsupported provider: {provider}")
            except Exception as e:
                logger.warning(f"LLM call failed: {e}")
                await asyncio.sleep(2 ** attempt)

        await self._record_failure()
        raise LLMServiceException("Failed to generate LLM response after retries")