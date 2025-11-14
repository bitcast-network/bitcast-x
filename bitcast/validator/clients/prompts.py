"""
Prompt templates for brief evaluation.

This module contains all prompt templates used for evaluating tweet content against briefs.
Each version represents a different evaluation approach.

How to add a new prompt version:
1. Create a new function generate_brief_evaluation_prompt_vX (where X is the version number)
2. Add the function to the PROMPT_GENERATORS registry
3. Update tests to validate the new version
4. Briefs can then specify "prompt_version": X to use the new format

Currently supported versions: v1 (default: v1)
"""

def generate_brief_evaluation_prompt_v1(brief, tweet):
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
        "**Important Context**\n"
        "• The brief requirements are **minimum requirements** - creators are may choose to go deeper into the topic area - although this is not mandatory\n"
        "Additional requirement: The tweet must not be negative or critical of the sponsor.\n"
        "**Step-by-step instructions**\n\n"
        "1. **Auto-number** each requirement in the brief (1, 2, 3 …) in the order it appears.\n"
        "2. For every numbered requirement:\n"
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
        "- Req 1: [requirement text] — Met / Not Met — \"quoted evidence\" (start-sec or range)\n"
        "- Req 2: ...\n"
        "...\n"
        "## Verdict\n"
        "YES or NO\n"
        "## Summary\n"
        "Brief 1 sentence explanation of why the content did or did not meet the brief requirements.\n"
        "```\n"
        "Be concise and remember: fabricated evidence = Not Met."
    )

# Registry of available prompt generators
PROMPT_GENERATORS = {
    1: generate_brief_evaluation_prompt_v1
}

def get_prompt_generator(version):
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
        raise ValueError(f"Unsupported prompt version: {version}. Available versions: {list(PROMPT_GENERATORS.keys())}")
    
    return PROMPT_GENERATORS[version]


def generate_brief_evaluation_prompt(brief, tweet, version=1):
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