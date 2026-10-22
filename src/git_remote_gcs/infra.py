import logging
import os
import sys
from dataclasses import dataclass

import structlog


class ConfigLoadError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    verbose: bool
    lock_ttl: int

    @classmethod
    def from_env(cls) -> "Config":
        raw_ttl = os.getenv("GIT_REMOTE_GCS_LOCK_TTL", "60")
        try:
            lock_ttl = int(raw_ttl)
        except ValueError as ex:
            raise ConfigLoadError(f"GIT_REMOTE_GCS_LOCK_TTL must be an integer, got '{raw_ttl}'") from ex
        if lock_ttl <= 0:
            raise ConfigLoadError(f"GIT_REMOTE_GCS_LOCK_TTL must be positive, got {lock_ttl}")
        return cls(verbose=os.getenv("GIT_REMOTE_GCS_VERBOSE", "0") == "1", lock_ttl=lock_ttl)


def init_logging(verbose: bool) -> None:
    # stdout carries the remote-helper protocol, so logs must only ever go to stderr.
    # Called again when git sends "option verbosity", hence no logger caching.
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if verbose else logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
