"""Typed domain errors used across Bitcast X v3."""


class BitcastXError(Exception):
    """Base class for expected Bitcast X failures."""


class ProtocolError(BitcastXError):
    """A protocol payload violates a deterministic rule."""


class AuthenticationError(BitcastXError):
    """A signed miner-validator request cannot be authenticated."""


class ChainOperationError(BitcastXError):
    """A required Bittensor read or transaction failed."""


class ResponseTooLargeError(BitcastXError):
    """A remote miner response exceeds the configured safety limit."""


class ReconciliationUnavailableError(BitcastXError):
    """Required chain, campaign, or X evidence is temporarily unavailable."""
