"""Data models for account connection."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class AccountMapping:
    """
    Mapping between social media account and network UID.
    
    Represents the connection between a miner's UID and their social
    media account (discovered via connection tags).
    """
    account_username: str
    uid: int
    pool: str
    connection_tag: Optional[str] = None
    hotkey: Optional[str] = None
    
    def __post_init__(self):
        """Validation after initialization."""
        if not self.account_username:
            raise ValueError("Account username cannot be empty")
        
        if self.uid < 0:
            raise ValueError(f"UID must be non-negative, got {self.uid}")
        
        if not self.pool:
            raise ValueError("Pool name cannot be empty")
    
    @classmethod
    def from_dict(cls, data: dict) -> 'AccountMapping':
        """Create AccountMapping from database/API dictionary."""
        return cls(
            account_username=data['account_username'],
            uid=data['uid'],
            pool=data.get('pool', 'tao'),
            connection_tag=data.get('connection_tag'),
            hotkey=data.get('hotkey')
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary format for compatibility."""
        return {
            'account_username': self.account_username,
            'uid': self.uid,
            'pool': self.pool,
            'connection_tag': self.connection_tag,
            'hotkey': self.hotkey
        }


__all__ = ['AccountMapping']

