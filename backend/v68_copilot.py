import json
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.getenv("JASONG_OPENAI_MODEL", "gpt-5.6")

class JasongCopilot:
    VERSION = "V6.8"

    def __init__(self):
        self.model = DEFAULT_MODEL

    def configured(self):
        return bool(os.getenv("OPENAI_API_KEY", "").strip())

    def _post_response(self, instructions, input_text, max_output_tokens=1200):
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured on the backend.")

        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": max_output_tokens,
        }
        req = urllib.request.Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API error {exc.code}: {body[:500]}") from exc

    @staticmethod
    def _extract_text(response):
        if isinstance(response.get("output_text"), str):
            return response["output_text"].strip()
        parts = []
        for item in response.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "\n".join(parts).strip()

    def analyze(self, question, context, mode="GENERAL"):
        instructions = """You are the V6.8 Intelligence Copilot inside Jasong AI Trader.
Analyze only evidence supplied by the Jasong AI backend.
Explain trades and non-trades; compare historical calibration with genuine forward performance;
identify patterns in wins/losses and deteriorating markets/directions/confidence buckets;
and suggest hypotheses/tests for improving the system.
You are advisory only. You cannot authorize, open, close or size broker trades.
Never claim guaranteed profit, never hide losses, and never recommend bypassing risk,
portfolio, forward-trust, calibration or execution gates. Treat small samples cautiously.
For overnight reviews separate settled trades from open/watch-only setups and report
wins/losses, WR, P&L, PF when supported, entry paths/confidence, repeated loss patterns,
what to watch next, and whether evidence is strong enough to justify a change.
Never expose secrets, API keys, credentials or server configuration."""
        evidence = {"mode": mode, "generated_at": time.time(), "evidence": context}
        response = self._post_response(
            instructions,
            "USER QUESTION:\n" + question + "\n\nJASONG AI EVIDENCE:\n" +
            json.dumps(evidence, separators=(",", ":"), default=str),
        )
        return {
            "version": self.VERSION,
            "model": self.model,
            "answer": self._extract_text(response),
            "response_id": response.get("id"),
            "advisory_only": True,
            "can_execute_trades": False,
            "live_execution": False,
        }

COPILOT = JasongCopilot()
