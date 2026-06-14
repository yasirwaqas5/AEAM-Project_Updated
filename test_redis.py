from aeam.integrations.redis_client import RedisClient
from aeam.config.settings import Settings

settings = Settings()
redis_client = RedisClient(str(settings.REDIS_URL))

print("Ping:", redis_client.ping())