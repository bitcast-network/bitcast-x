"""
Referral code encoding and decoding utilities.

Referral codes are generated from X handles using:
1. Base64 encoding using MySQL's TO_BASE64()
2. Convert to URL-safe Base64url format by replacing + with - and / with _
3. Remove = padding with TRIM(TRAILING '=')
"""

import base64
from typing import Optional


def encode_referral_code(x_handle: str) -> str:
    """
    Encode an X handle to a URL-safe Base64 referral code.

    Args:
        x_handle: The X handle to encode (e.g., "bitcast_network")

    Returns:
        URL-safe Base64 encoded string without padding

    Example:
        >>> encode_referral_code("bitcast_network")
        'Yml0Y2FzdF9uZXR3b3Jr'
    """
    # Remove @ prefix if present
    handle = x_handle.lstrip('@')

    # Encode to Base64
    encoded = base64.b64encode(handle.encode('utf-8'))

    # Convert to URL-safe Base64url format
    # Replace + with - and / with _
    url_safe = encoded.decode('utf-8').replace('+', '-').replace('/', '_')

    # Remove padding
    return url_safe.rstrip('=')


def decode_referral_code(code: str) -> Optional[str]:
    """
    Decode a URL-safe Base64 referral code back to an X handle.

    Args:
        code: The URL-safe Base64 encoded referral code

    Returns:
        The original X handle, or None if the code is invalid

    Example:
        >>> decode_referral_code('Yml0Y2FzdF9uZXR3b3Jr')
        'bitcast_network'
    """
    if not code:
        return None

    try:
        # Add padding back if needed
        padding_needed = (4 - len(code) % 4) % 4
        padded = code + '=' * padding_needed

        # Convert from URL-safe Base64url format
        # Replace - with + and _ with /
        standard = padded.replace('-', '+').replace('_', '/')

        # Decode from Base64
        decoded = base64.b64decode(standard)

        return decoded.decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return None
