import os
import logging
from logging.handlers import RotatingFileHandler

EVENTS_LEVEL_NUM = 38
DEFAULT_LOG_BACKUP_COUNT = 10


def setup_events_logger(full_path, events_retention_size, mechid=None):
    """
    Setup events logger with optional mechanism ID in filename.
    
    Args:
        full_path: Base directory for log files
        events_retention_size: Maximum size of log files before rotation
        mechid: Optional mechanism ID to include in filename (default: None)
    """
    logging.addLevelName(EVENTS_LEVEL_NUM, "EVENT")

    logger = logging.getLogger("event")
    logger.setLevel(EVENTS_LEVEL_NUM)

    def event(self, message, *args, **kws):
        if self.isEnabledFor(EVENTS_LEVEL_NUM):
            self._log(EVENTS_LEVEL_NUM, message, args, **kws)

    logging.Logger.event = event

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Include mechanism ID in filename if provided
    log_filename = f"events_mech_{mechid}.log" if mechid is not None else "events.log"
    
    file_handler = RotatingFileHandler(
        os.path.join(full_path, log_filename),
        maxBytes=events_retention_size,
        backupCount=DEFAULT_LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(EVENTS_LEVEL_NUM)
    logger.addHandler(file_handler)

    return logger
