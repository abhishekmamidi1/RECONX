from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.models import Transaction
from app.services.reasoning.base import (
    ReasoningAnalysis,
    ReasoningDecision,
    build_analysis_prompt,
    build_batch_prompt,
    build_prompt,
    fallback_analysis,
    fallback_decision,
)

logger = logging.getLogger(__name__)


class OllamaReasoningAgent:
    def __init__(self, api_url: str, model: str, timeout_s: float):
        self.api_url = api_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    async def decide(
        self,
        txn: Transaction,
        candidates: list[tuple[Transaction, object]],
        policy: dict,
    ) -> ReasoningDecision:
        system, user = build_prompt(txn, candidates, policy)
        parsed = await self._chat_json(system, user)
        if not parsed:
            return fallback_decision("empty/unparseable response")
        try:
            decision = ReasoningDecision(
                decision=parsed["decision"],
                confidence=float(parsed["confidence"]),
                rationale=str(parsed["rationale"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return fallback_decision(f"malformed response: {exc!s}"[:200])
        if decision.decision not in ("match", "no_match", "needs_human"):
            return fallback_decision(f"invalid decision verb: {decision.decision}")
        return decision

    async def decide_batch(
        self,
        bank: Transaction,
        components: list[Transaction],
        policy: dict,
    ) -> ReasoningDecision:
        system, user = build_batch_prompt(bank, components, policy)
        parsed = await self._chat_json(system, user)
        if not parsed:
            return fallback_decision("empty/unparseable response")
        try:
            decision = ReasoningDecision(
                decision=parsed["decision"],
                confidence=float(parsed["confidence"]),
                rationale=str(parsed["rationale"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return fallback_decision(f"malformed response: {exc!s}"[:200])
        if decision.decision not in ("match", "no_match", "needs_human"):
            return fallback_decision(f"invalid decision verb: {decision.decision}")
        return decision

    async def analyze(
        self,
        txn: Transaction,
        policy: dict,
        *,
        missing_sources: list[str] | None = None,
        references: list[tuple[Transaction, object]] | None = None,
    ) -> ReasoningAnalysis:
        system, user = build_analysis_prompt(
            txn, policy, missing_sources=missing_sources, references=references
        )
        parsed = await self._chat_json(system, user)
        if not parsed:
            return fallback_analysis("empty/unparseable response")
        try:
            analysis = ReasoningAnalysis(
                classification=parsed["classification"],
                confidence=float(parsed["confidence"]),
                rationale=str(parsed["rationale"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return fallback_analysis(f"malformed response: {exc!s}"[:200])
        if analysis.classification not in (
            "likely_pending",
            "data_quality",
            "manual_investigation",
        ):
            return fallback_analysis(f"invalid classification: {analysis.classification}")
        return analysis

    async def _chat_json(self, system: str, user: str) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    f"{self.api_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "stream": False,
                        "format": "json",
                        "options": {"temperature": 0},
                    },
                )
                response.raise_for_status()
            content = response.json()["message"]["content"]
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except Exception as exc:
            logger.warning("reasoning agent failed: %s", exc)
            return None
