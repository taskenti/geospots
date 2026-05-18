import os
from dataclasses import dataclass

@dataclass
class Config:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    request_delay: float
    max_workers: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_host=os.environ["DB_HOST"],
            db_port=int(os.environ.get("DB_PORT", "5432")),
            db_name=os.environ["DB_NAME"],
            db_user=os.environ["DB_USER"],
            db_password=os.environ["DB_PASSWORD"],
            request_delay=float(os.environ.get("REQUEST_DELAY_SECONDS", "2")),
            max_workers=int(os.environ.get("MAX_WORKERS", "3")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )

    @property
    def db_dsn(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
