from app.services.reasoning.heuristic_agent import HeuristicReasoningAgent
from app.services.reasoning.ollama_agent import OllamaReasoningAgent


def get_agent(settings):
    if settings.reasoning_provider == "ollama":
        return OllamaReasoningAgent(
            settings.reasoning_api_url,
            settings.reasoning_model,
            settings.reasoning_timeout_s,
        )
    return HeuristicReasoningAgent()


__all__ = [
    "HeuristicReasoningAgent",
    "OllamaReasoningAgent",
    "get_agent",
]
