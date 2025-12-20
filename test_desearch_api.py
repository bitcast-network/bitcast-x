#!/usr/bin/env python3
"""
Quick test script for Desearch.ai API
"""

import requests
import os
from pathlib import Path

# Load .env file manually
env_path = Path(__file__).parent / "bitcast" / "validator" / ".env"
api_key = None

if env_path.exists():
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('DESEARCH_API_KEY=') and not line.startswith('#'):
                api_key = line.split('=', 1)[1].strip().strip('"').strip("'")
                break
else:
    # Try environment variable directly
    api_key = os.getenv('DESEARCH_API_KEY')

if not api_key:
    print("ERROR: DESEARCH_API_KEY not found in .env file")
    print(f"   Looking for .env at: {env_path}")
    exit(1)

print(f"[OK] Found API key: {api_key[:10]}...{api_key[-5:] if len(api_key) > 15 else '***'}")
print(f"   Full key length: {len(api_key)} characters")
print(f"   Key starts with $: {api_key.startswith('$')}")
print()

# Test username
username = "elonmusk"
url = f"https://api.desearch.ai/twitter/user/posts"
params = {"username": username}

# Build authorization header
if api_key.startswith('dt_$'):
    # Already has full prefix, use as-is
    auth_header = api_key
elif api_key.startswith('$'):
    # Has $ but missing dt_ prefix
    auth_header = "dt_" + api_key
else:
    # No prefix, add dt_$
    auth_header = "dt_$" + api_key

headers = {
    "Authorization": auth_header,
    "Content-Type": "application/json"
}

print(f"[TEST] Testing Desearch.ai API")
print(f"   URL: {url}")
print(f"   Params: {params}")
print(f"   Authorization header: {auth_header}")
print(f"   Authorization (masked): {auth_header[:15]}...{auth_header[-5:]}")
print()

try:
    print("[REQUEST] Making request...")
    response = requests.get(url, headers=headers, params=params, timeout=30)
    
    print(f"[RESPONSE] Status: {response.status_code}")
    print(f"   Response Headers: {dict(response.headers)}")
    print()
    
    if response.status_code == 200:
        data = response.json()
        print("[SUCCESS] Request succeeded!")
        print(f"   Response keys: {list(data.keys()) if isinstance(data, dict) else 'List response'}")
        
        if isinstance(data, dict):
            if 'tweets' in data:
                print(f"   Number of tweets: {len(data['tweets'])}")
            if 'user' in data:
                user = data['user']
                print(f"   User: @{user.get('username', 'N/A')}")
                print(f"   Followers: {user.get('followers_count', 'N/A')}")
        
        # Show first tweet if available
        if isinstance(data, dict) and 'tweets' in data and len(data['tweets']) > 0:
            first_tweet = data['tweets'][0]
            print(f"\n   First tweet:")
            print(f"   - ID: {first_tweet.get('id', 'N/A')}")
            print(f"   - Text: {first_tweet.get('text', 'N/A')[:100]}...")
            print(f"   - Created: {first_tweet.get('created_at', 'N/A')}")
    else:
        print(f"[ERROR] Status: {response.status_code}")
        try:
            error_data = response.json()
            print(f"   Error response: {error_data}")
        except:
            print(f"   Error text: {response.text[:500]}")
            
except requests.exceptions.RequestException as e:
    print(f"[ERROR] Request failed: {e}")
except Exception as e:
    print(f"[ERROR] Unexpected error: {e}")
    import traceback
    traceback.print_exc()

