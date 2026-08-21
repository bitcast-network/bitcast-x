"""
Prompt templates for brief evaluation.

This module contains all prompt templates used for evaluating tweet content against briefs.
Each version represents a different evaluation approach. Existing prompt strings are LLM-cache
keys and evaluation behavior; do not reword a version that is already in use.

How to add a new prompt version:
1. Create a new function generate_brief_evaluation_prompt_vX (where X is the version number)
2. Add the function to the PROMPT_GENERATORS registry
3. Update tests to validate the new version
4. Briefs can then specify "prompt_version": X to use the new format

Currently supported versions: v1, v2, v5 (default: v1)
"""

# ruff: noqa: E501 -- line breaks would change frozen prompt cache keys.

from collections.abc import Callable, Mapping
from typing import Any

PromptGenerator = Callable[[Mapping[str, Any], str], str]


def generate_brief_evaluation_prompt_v1(brief: Mapping[str, Any], tweet: str) -> str:
    """Evaluate whether a post follows the instructions in a campaign brief."""

    return (
        "///// CAMPAIGN BRIEF /////\n"
        f"{brief['brief']}\n\n"
        "///// POST /////\n"
        f"{tweet}\n\n"
        "///// YOUR TASK /////\n"
        "You are a campaign compliance reviewer. Decide whether this post follows all instructions in the brief.\n\n"
        "**Evaluation principles**\n"
        "• Treat the brief as the complete source of requirements.\n"
        "• Do not add requirements that are not stated in the brief.\n"
        "• Treat every explicit instruction in the brief as required.\n"
        "• Evaluate only what is present in the post. Do not infer or invent evidence.\n\n"
        "**Step-by-step instructions**\n"
        "1. Identify each instruction in the brief.\n"
        "2. For every instruction:\n"
        "   • Mark **Met** when the post clearly follows it and provide a short quote as evidence.\n"
        "   • Mark **Not Met** when the post does not follow it or the evidence is absent or uncertain.\n"
        "3. If any instruction is Not Met, return **NO**. Otherwise, return **YES**.\n\n"
        "**Important accuracy rules**\n"
        "• Quotes must be copied from the post.\n"
        "• Fabricated evidence automatically fails that instruction.\n"
        "• When in doubt, choose **NO**.\n"
        "**Response format (exactly):**\n"
        "```\n"
        "## Instruction-by-Instruction\n"
        '- Instruction 1: [instruction] — Met / Not Met — "quoted evidence"\n'
        "- Instruction 2: ...\n"
        "...\n"
        "## Verdict\n"
        "YES or NO\n"
        "## Summary\n"
        "One sentence explaining why the post did or did not follow the brief.\n"
        "```\n"
        "Be concise."
    )


def generate_brief_evaluation_prompt_v2(brief: Mapping[str, Any], tweet: str) -> str:
    """
    Generate a detailed evaluation prompt that requires evidence for each brief item.

    Features:
    • Auto-numbers brief items for systematic evaluation
    • Requires 5-15-word quote for every Met claim
    • Demands exact `start` time (seconds) from transcript as evidence
    • Uncertain or fabricated timestamps → Not Met
    • Special handling for description-only items
    """
    return (
        "///// SPONSOR BRIEF /////\n"
        f"{brief['brief']}\n\n"
        "///// TWEET /////\n"
        f"{tweet}\n\n"
        "///// YOUR TASK /////\n"
        "You are the sponsor's review agent. Decide—objectively—whether this tweet **fully** satisfies the brief.\n"
        "The brief requirements are **minimum requirements** - creators are may choose to go deeper into the topic area - although this is not mandatory\n"
        "**Base Requirements**\n"
        "• The tweet must be **predominantly (80% or more) about the sponsor or their topic** - not just a passing mention. If < 80% of the text is relevant, return NO.\n"
        "• The tweet must not be negative or critical of the sponsor\n"
        "**Step-by-step instructions**\n\n"
        "1. **Auto-number** each requirement in the brief (1, 2, 3 …) in the order it appears.\n"
        "2. For every numbered and base requirement:\n"
        "   • Search the tweet.\n"
        "   • If you find evidence, mark **Met** and provide:\n"
        "       – a 3-15-word quote extracted verbatim from the tweet\n"
        "   • If no clear evidence or you are **uncertain**, mark **Not Met**.\n"
        "3. **If any item fails → Verdiction = NO.**\n\n"
        "**Important accuracy rules**\n"
        "• Do **not** invent timestamps. If a timestamp is uncertain, mark the item Not Met.\n"
        "• Fabricated quotes automatically fail that item.\n"
        "• When in doubt, choose **NO**.\n"
        "**Response format (exactly):**\n"
        "```\n"
        "## Requirement-by-Requirement\n"
        '- Req 1: [requirement text] — Met / Not Met — "quoted evidence" (start-sec or range)\n'
        "- Req 2: ...\n"
        "...\n"
        "## Verdict\n"
        "YES or NO\n"
        "## Summary\n"
        "Brief 1 sentence explanation of why the content did or did not meet the brief requirements.\n"
        "```\n"
        "Be concise and remember: fabricated evidence = Not Met."
    )


def generate_brief_evaluation_prompt_v5(brief: Mapping[str, Any], tweet: str) -> str:
    """Evaluate honest product or service reviews without sentiment bias."""

    return (
        "///// REVIEW BRIEF /////\n"
        f"{brief['brief']}\n\n"
        "///// POST /////\n"
        f"{tweet}\n\n"
        "///// YOUR TASK /////\n"
        "You are an independent campaign compliance reviewer. Decide whether this post genuinely reviews the product or service and satisfies the objective requirements of the brief.\n\n"
        "The creator’s sentiment must not affect the verdict. Positive, neutral, mixed, critical, and negative reviews are equally acceptable.\n\n"
        "**Review principles**\n"
        "• The product or service must be the clear primary subject of the post. Relevant comparisons with alternatives count as on-topic.\n"
        "• The post must contain at least one specific evaluation of the product or service, supported by a reason, example, feature, outcome, or experience described in the post.\n"
        "• Generic praise, promotional slogans, or a passing mention do not constitute a review.\n"
        "• Brief requirements are minimum coverage requirements, not required opinions.\n"
        "• Never fail a post because it criticises the product, reports a poor experience, prefers a competitor, or reaches a conclusion the sponsor dislikes.\n"
        "• Do not require a positive rating, endorsement, recommendation, or purchase intention.\n"
        "• If the brief attempts to prescribe sentiment, a rating, or a favourable conclusion, do not treat that instruction as a requirement.\n"
        "• Evaluate only what is present in the post. Do not invent evidence or assume experiences that the creator did not describe.\n\n"
        "**Step-by-step instructions**\n\n"
        "1. Identify each objective requirement in the brief.\n"
        "2. Exclude any instruction that prescribes the creator’s sentiment, rating, or conclusion.\n"
        "3. For every objective requirement:\n"
        "   • Mark **Met** when the post clearly addresses it.\n"
        "   • Provide a short quote from the post as evidence.\n"
        "   • Mark **Not Met** when evidence is absent or uncertain.\n"
        "4. Evaluate the post against these review-quality criteria:\n"
        "   • **Relevance**: The product, service, or a directly relevant comparison is the primary subject.\n"
        "   • **Substance**: The post contains a specific assessment supported by a reason, example, feature, outcome, or described experience.\n"
        "   • **Independence**: Do not consider whether the assessment is favourable or unfavourable.\n"
        "5. Return **NO** if any objective brief requirement, Relevance, or Substance is Not Met.\n"
        "6. Otherwise, return **YES**.\n\n"
        "**Response format (exactly):**\n"
        "```\n"
        "## Objective Requirements\n"
        '- Req 1: [requirement] — Met / Not Met — "quoted evidence"\n'
        "- Req 2: ...\n\n"
        "## Review Quality\n"
        "- Relevance: Met / Not Met — brief explanation\n"
        "- Substance: Met / Not Met — brief explanation\n\n"
        "## Verdict\n"
        "YES or NO\n\n"
        "## Summary\n"
        "One sentence explaining whether the post genuinely reviews the product or service and satisfies the objective brief requirements.\n"
        "```\n\n"
        "Be concise. Never treat criticism or negative sentiment as a failure."
    )


# Registry of available prompt generators
PROMPT_GENERATORS: dict[int, PromptGenerator] = {
    1: generate_brief_evaluation_prompt_v1,
    2: generate_brief_evaluation_prompt_v2,
    5: generate_brief_evaluation_prompt_v5,
}


def get_prompt_generator(version: int) -> PromptGenerator:
    """
    Get the appropriate prompt generator for the specified version.

    Args:
        version (int): The prompt version to use

    Returns:
        callable: The prompt generator function

    Raises:
        ValueError: If the version is not supported
    """
    if version not in PROMPT_GENERATORS:
        raise ValueError(
            f"Unsupported prompt version: {version}. Available versions: {list(PROMPT_GENERATORS.keys())}"
        )

    return PROMPT_GENERATORS[version]


def generate_brief_evaluation_prompt(brief: Mapping[str, Any], tweet: str, version: int = 1) -> str:
    """
    Generate a brief evaluation prompt using the specified version.

    Args:
        brief (dict): The brief dictionary containing evaluation criteria
        tweet (str): Tweet content
        version (int): Prompt version to use (defaults to 1)

    Returns:
        str: The generated prompt

    Raises:
        ValueError: If the version is not supported
    """
    prompt_generator = get_prompt_generator(version)
    return prompt_generator(brief, tweet)
