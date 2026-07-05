from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_url: str = "./dev.db"
    redis_url: str = "redis://redis:6379/0"
    ml_service_url: str = "http://ml-service:8000"
    storage_path: str = "/data"
    chunk_size_bytes: int = 20971520

    redis_max_connections: int = 20
    redis_socket_timeout: float = 2.0
    redis_health_check_interval: int = 30
    redis_cache_ttl: int = 5

    # API authentication — static key checked in middleware + nginx.
    # If empty, auth is disabled (useful for local dev).
    api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def sqlite_db_path(self) -> str:
        """Returns the raw file path of the SQLite database file (without sqlite:/// prefix)."""
        path = self.db_url
        if path.startswith("sqlite:///"):
            return path[len("sqlite:///") :]
        elif path.startswith("sqlite://"):
            return path[len("sqlite://") :]
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
