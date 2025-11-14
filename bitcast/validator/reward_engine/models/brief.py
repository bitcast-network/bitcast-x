"""Brief model for campaign representation."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Brief:
    """
    Campaign brief with validated structure.
    
    Represents a marketing campaign that miners can participate in by
    creating content (tweets, videos, etc.) that meets the brief criteria.
    """
    id: str
    pool: str
    budget: float
    start_date: datetime
    end_date: datetime
    brief_text: str
    tag: Optional[str] = None
    qrt: Optional[str] = None
    prompt_version: int = 1
    boost: float = 1.0
    max_tweets: Optional[int] = None
    
    def __post_init__(self):
        """Validation after initialization."""
        if self.budget < 0:
            raise ValueError(f"Budget must be non-negative, got {self.budget}")
        
        if self.end_date < self.start_date:
            raise ValueError(
                f"End date ({self.end_date}) must be after start date ({self.start_date})"
            )
        
        if not self.id:
            raise ValueError("Brief ID cannot be empty")
        
        if not self.pool:
            raise ValueError("Pool name cannot be empty")
    
    @property
    def daily_budget(self) -> float:
        """Calculate daily budget over emissions period."""
        from bitcast.validator.utils.config import EMISSIONS_PERIOD
        return self.budget / EMISSIONS_PERIOD
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Brief':
        """
        Create Brief from API response dictionary.
        
        Args:
            data: Dictionary from API (e.g., get_briefs() response)
            
        Returns:
            Brief instance
            
        Raises:
            ValueError: If required fields missing or invalid
        """
        # Parse datetime strings to timezone-aware UTC
        start_date = data.get('start_date')
        if isinstance(start_date, str):
            try:
                start_date = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                start_date = start_date.astimezone(timezone.utc)
            except (ValueError, AttributeError):
                start_date = datetime.strptime(start_date, '%Y-%m-%d')
                start_date = start_date.replace(tzinfo=timezone.utc)
        
        end_date = data.get('end_date')
        if isinstance(end_date, str):
            try:
                end_date = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                end_date = end_date.astimezone(timezone.utc)
            except (ValueError, AttributeError):
                end_date = datetime.strptime(end_date, '%Y-%m-%d')
                end_date = end_date.replace(tzinfo=timezone.utc)
        
        return cls(
            id=data['id'],
            pool=data.get('pool', 'tao'),
            budget=float(data.get('budget', 0)),
            start_date=start_date,
            end_date=end_date,
            brief_text=data.get('brief', ''),
            tag=data.get('tag'),
            qrt=data.get('qrt'),
            prompt_version=int(data.get('prompt_version', 1)),
            boost=float(data.get('boost', 1.0)),
            max_tweets=data.get('max_tweets')
        )
    
    def to_dict(self) -> dict:
        """Convert back to dictionary format for compatibility."""
        return {
            'id': self.id,
            'pool': self.pool,
            'budget': self.budget,
            'start_date': self.start_date.isoformat() if hasattr(self.start_date, 'isoformat') else str(self.start_date),
            'end_date': self.end_date.isoformat() if hasattr(self.end_date, 'isoformat') else str(self.end_date),
            'brief': self.brief_text,
            'tag': self.tag,
            'qrt': self.qrt,
            'prompt_version': self.prompt_version,
            'boost': self.boost,
            'max_tweets': self.max_tweets
        }

