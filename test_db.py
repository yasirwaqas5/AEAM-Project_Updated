from aeam.integrations.database import DatabaseClient
from aeam.config.settings import Settings

settings = Settings()
db = DatabaseClient(str(settings.DATABASE_URL))

db.execute("SELECT 1")
print("PostgreSQL connection valid.")