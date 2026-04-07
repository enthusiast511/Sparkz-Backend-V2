from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # OpenAI
    OPENAI_API_KEY: str
    OPENAI_MODEL: str = "gpt-4o"
    REVIEW_MODEL: str = "gpt-4o-mini"

    # Processing
    BATCH_SIZE: int = 12
    MAX_RETRIES: int = 3
    TEMPERATURE: float = 0.1

    # Paths
    UPLOAD_DIR: str = "./data/uploads"
    DATABASE_URL: str = "sqlite:///./data/sparkz.db"

    # CORS: comma-separated origins (e.g. https://spark-frontend-v2.vercel.app)
    # Local dev origins are always included in main.py
    CORS_ALLOWED_ORIGINS: str = ""

    # Context limits
    MAX_TOKENS_FULL_DOC: int = 80000

    # PII redaction before LLM (company numbers, amounts, postcodes, ORG/GPE never touched)
    # none    — no redaction
    # minimal — email + UK phone only (default; best for rule-check quality)
    # names   — minimal + spaCy PERSON (policy-driven; can hide director names)
    REDACTION_MODE: Literal["none", "minimal", "names"] = "minimal"

    class Config:
        env_file = ".env"


settings = Settings()
