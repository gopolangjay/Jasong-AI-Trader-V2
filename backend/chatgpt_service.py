"""
ChatGPT Integration Service for Jasong AI Trader V2
Provides intelligent analysis of trading signals and market conditions
"""

import os
from typing import Optional, Dict
from openai import OpenAI


class ChatGPTAnalyzer:
    """
    Provides ChatGPT-powered analysis for trading signals and decisions
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize ChatGPT analyzer with OpenAI API key"""
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            self.client = None
            self.available = False
        else:
            self.client = OpenAI(api_key=self.api_key)
            self.available = True
        
        self.model = "gpt-4o-mini"
        self.temperature = 0.7
        self.max_tokens = 500
    
    def is_available(self) -> bool:
        """Check if ChatGPT integration is available"""
        return self.available
    
    def analyze_trade_signal(self, indicators: Dict, decision: Dict = None) -> Dict:
        """
        Analyze trading indicators and provide AI interpretation
        
        Args:
            indicators: Dictionary containing EMA, RSI, MACD, Bollinger, ATR, etc.
            decision: Optional existing decision dict from rule engine
            
        Returns:
            Dictionary with AI analysis, confidence, and recommendation
        """
        if not self.available:
            return {
                "status": "unavailable",
                "error": "OpenAI API key not configured",
                "recommendation": "WAIT"
            }
        
        # Build context from indicators
        context_parts = []
        if indicators:
            for key, value in indicators.items():
                if not key.startswith("_"):  # Skip internal fields
                    context_parts.append(f"{key}: {value}")
        
        context_str = "\n".join(context_parts[:15])  # Limit to 15 top indicators
        
        # Build the analysis prompt
        existing_decision = ""
        if decision:
            direction = decision.get("direction", "WAIT")
            confidence = decision.get("confidence", 0.0)
            existing_decision = f"\nCurrent rule-based decision: {direction} (confidence: {confidence:.1%})"
        
        prompt = f"""You are a professional trading analyst. Analyze these market indicators and provide a trading recommendation.

Market Indicators:
{context_str}
{existing_decision}

Provide your analysis in exactly this JSON format:
{{
    "recommendation": "BUY" or "SELL" or "WAIT",
    "confidence": 0.0 to 1.0,
    "rationale": "Brief explanation (1-2 sentences)",
    "risk_level": "LOW", "MEDIUM", or "HIGH",
    "key_signals": ["signal 1", "signal 2", "signal 3"]
}}

Respond with ONLY the JSON object, no additional text."""
        
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=self.temperature
            )
            
            response_text = response.content[0].text.strip()
            
            # Parse JSON response
            import json
            analysis = json.loads(response_text)
            
            return {
                "status": "success",
                "recommendation": analysis.get("recommendation", "WAIT"),
                "confidence": float(analysis.get("confidence", 0.0)),
                "rationale": analysis.get("rationale", ""),
                "risk_level": analysis.get("risk_level", "MEDIUM"),
                "key_signals": analysis.get("key_signals", []),
                "model": self.model
            }
            
        except json.JSONDecodeError:
            return {
                "status": "error",
                "error": "Invalid JSON response from ChatGPT",
                "recommendation": "WAIT"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "recommendation": "WAIT"
            }
    
    def explain_decision(self, decision: Dict, indicators: Dict) -> str:
        """
        Get ChatGPT explanation of a trading decision
        
        Args:
            decision: The trade decision dictionary
            indicators: Market indicators used in the decision
            
        Returns:
            String explanation
        """
        if not self.available:
            return "ChatGPT service is not available"
        
        direction = decision.get("direction", "WAIT")
        confidence = decision.get("confidence", 0.0)
        
        prompt = f"""Explain this trading decision in simple terms for a trader:

Decision: {direction} with {confidence:.1%} confidence

Key Indicators: {str(indicators)[:500]}

Provide a brief, clear explanation of why this decision was made and what it means for trading."""
        
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5
            )
            return response.content[0].text.strip()
        except Exception as e:
            return f"Could not generate explanation: {str(e)}"
    
    def compare_with_rules(self, rule_decision: Dict, ai_decision: Dict, indicators: Dict) -> Dict:
        """
        Compare AI decision with rule-based decision
        
        Args:
            rule_decision: Decision from traditional rule engine
            ai_decision: ChatGPT analysis
            indicators: Market indicators
            
        Returns:
            Comparison analysis and recommended approach
        """
        if not self.available:
            return {
                "status": "unavailable",
                "recommendation": rule_decision.get("direction", "WAIT")
            }
        
        prompt = f"""Compare these two trading analyses and recommend the best approach:

Rule-Based Decision:
- Direction: {rule_decision.get('direction', 'WAIT')}
- Confidence: {rule_decision.get('confidence', 0.0):.1%}

AI Analysis:
- Recommendation: {ai_decision.get('recommendation', 'WAIT')}
- Confidence: {ai_decision.get('confidence', 0.0):.1%}
- Rationale: {ai_decision.get('rationale', '')}

Indicators: {str(list(indicators.keys()))}

Provide a JSON response with:
{{
    "agreement": true/false,
    "recommended_direction": "BUY/SELL/WAIT",
    "final_confidence": 0.0-1.0,
    "reasoning": "Why combine these approaches?",
    "weight_rule_vs_ai": "percentage rule vs AI weight"
}}"""
        
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6
            )
            
            import json
            result = json.loads(response.content[0].text.strip())
            return {
                "status": "success",
                **result
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "recommended_direction": rule_decision.get("direction", "WAIT")
            }


# Global instance
_analyzer_instance: Optional[ChatGPTAnalyzer] = None


def get_analyzer() -> ChatGPTAnalyzer:
    """Get or create the global ChatGPT analyzer instance"""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = ChatGPTAnalyzer()
    return _analyzer_instance
