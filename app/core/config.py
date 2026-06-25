from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    db_url: str = "./dev.db"
    redis_url: str = "redis://localhost:6379/0"
    ml_service_url: str = "http://ml-service:8000"
    storage_path: str = "/data"
    chunk_size_bytes: int = 20971520

    redis_max_connections: int = 20
    redis_socket_timeout: int = 5
    redis_health_check_interval: int = 30
    redis_cache_ttl: int = 5

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
