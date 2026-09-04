from pydantic import BaseModel


class DatabaseHealth(BaseModel):
    connected: bool
    latency_ms: float | None = None
    error: str | None = None


class AIHealth(BaseModel):
    reasoning_provider: str
    reasoning_model: str
    reasoning_mode: str
    embedding_provider: str
    embedding_model: str


class HealthResponse(BaseModel):
    status: str
    app_version: str
    env: str
    database: DatabaseHealth
    ai: AIHealth


class LivenessResponse(BaseModel):
    status: str
