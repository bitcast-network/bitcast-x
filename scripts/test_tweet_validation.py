#!/usr/bin/env python3
"""
Simple script to test tweet validation using Chutes API.

Usage:
    source ~/venv_bitcast_x/bin/activate
    python scripts/test_tweet_validation.py
"""

import sys
import os

# Disable LLM caching for fresh results every time
os.environ['DISABLE_LLM_CACHING'] = 'true'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bittensor as bt
from bitcast.validator.clients.ChuteClient import evaluate_content_against_brief
from bitcast.validator.reward_engine.utils.brief_fetcher import get_briefs

def get_brief_by_id(brief_id):
    """Fetch a brief from the API by ID."""
    try:
        briefs = get_briefs()
        for brief in briefs:
            if brief['id'] == brief_id:
                return brief
        return None
    except Exception as e:
        bt.logging.error(f"Error fetching briefs: {e}")
        return None

def main():
    bt.logging.set_info()
    
    print("\n" + "="*70)
    print("TWEET VALIDATION TESTER")
    print("="*70)
    
    # Get brief (ID or text)
    print("\n1. Enter BRIEF ID or paste BRIEF text, then press Enter twice:\n")
    brief_lines = []
    empty_count = 0
    while empty_count < 1:
        try:
            line = input()
            if line:
                brief_lines.append(line)
                empty_count = 0
            else:
                empty_count += 1
        except EOFError:
            break
    
    brief_input = "\n".join(brief_lines).strip()
    if not brief_input:
        print("\n❌ Brief cannot be empty!")
        return 1
    
    # Check if input is a brief ID (single line, no newlines)
    if '\n' not in brief_input and len(brief_input) < 50:
        print(f"\nFetching brief '{brief_input}' from API...")
        brief_obj = get_brief_by_id(brief_input)
        if brief_obj:
            brief_text = brief_obj.get('brief', '')
            brief_id = brief_obj['id']
            prompt_version = brief_obj.get('prompt_version', 1)
            print(f"✓ Found brief: {brief_id}")
            print(f"  Brief text: {brief_text[:100]}..." if len(brief_text) > 100 else f"  Brief text: {brief_text}")
        else:
            print(f"\n❌ Brief ID '{brief_input}' not found. Using as brief text instead.")
            brief_text = brief_input
            brief_id = 'test'
            prompt_version = 1
    else:
        brief_text = brief_input
        brief_id = 'test'
        prompt_version = 1
    
    # Get tweet
    print("\n2. Paste your TWEET text, then press Enter twice:\n")
    tweet_lines = []
    empty_count = 0
    while empty_count < 1:
        try:
            line = input()
            if line:
                tweet_lines.append(line)
                empty_count = 0
            else:
                empty_count += 1
        except EOFError:
            break
    
    tweet_text = "\n".join(tweet_lines).strip()
    if not tweet_text:
        print("\n❌ Tweet cannot be empty!")
        return 1
    
    # Evaluate
    print("\n" + "="*70)
    print("EVALUATING...")
    print("="*70 + "\n")
    
    brief = {'id': brief_id, 'brief': brief_text, 'prompt_version': prompt_version}
    
    try:
        meets_brief, reasoning, detailed_breakdown = evaluate_content_against_brief(brief, tweet_text)
        
        print("\n" + "="*70)
        if meets_brief:
            print("✅ VERDICT: YES - Tweet MEETS the brief")
        else:
            print("❌ VERDICT: NO - Tweet DOES NOT meet the brief")
        print("="*70)
        print(f"\nBRIEF ID: {brief_id}")
        print(f"PROMPT VERSION: v{prompt_version}")
        
        if detailed_breakdown:
            print("\nDETAILED BREAKDOWN:")
            print(detailed_breakdown)
        
        print("\nSUMMARY:")
        print(reasoning)
        print("\n" + "="*70 + "\n")
        
        return 0
    except Exception as e:
        print(f"\n❌ ERROR: {e}\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())

