"""
API clients for external services.

Contains clients for LLM evaluation (Chutes/OpenRouter) and Twitter API
integration with multi-provider support.

LLM provider is selected via the LLM_PROVIDER env var ('chutes' or 'openrouter').
Import evaluate_content_against_brief and check_for_prompt_injection from this
module — the factory routes to the correct backend automatically.
"""

from .twitter_client import TwitterClient
from .twitter_provider import TwitterProvider
from .desearch_provider import DesearchProvider
from .rapidapi_provider import RapidAPIProvider

from bitcast.validator.utils.config import LLM_PROVIDER

# Lazy LLM provider selection
_LLM_MODULE = None


def _get_llm_module():
    """Lazy-load the configured LLM client module."""
    global _LLM_MODULE
    if _LLM_MODULE is None:
        if LLM_PROVIDER == "openrouter":
            from . import OpenRouterClient as mod
        elif LLM_PROVIDER == "chutes":
            from . import ChuteClient as mod
        else:
            raise ValueError(
                f"Unsupported LLM_PROVIDER: '{LLM_PROVIDER}'. "
                "Use 'chutes' or 'openrouter'."
            )
        _LLM_MODULE = mod
    return _LLM_MODULE


def evaluate_content_against_brief(*args, **kwargs):
    """Evaluate content against a brief using the configured LLM provider."""
    return _get_llm_module().evaluate_content_against_brief(*args, **kwargs)


def check_for_prompt_injection(*args, **kwargs):
    """Check for prompt injection using the configured LLM provider."""
    return _get_llm_module().check_for_prompt_injection(*args, **kwargs)


__all__ = [
    "TwitterClient",
    "TwitterProvider",
    "DesearchProvider",
    "RapidAPIProvider",
    "evaluate_content_against_brief",
    "check_for_prompt_injection",
]
