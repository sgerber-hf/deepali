r"""Auxiliary functions to set up logging in main scripts."""

from enum import Enum
import logging


class LogLevel(str, Enum):
    r"""Enumeration of logging levels for use as type annotation when using Typer."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"

    def __int__(self) -> int:
        r"""Cast enumeration to int logging level."""
        return int(getattr(logging, self.value))


def configure_logging(log, args):
    r"""Initialize logging."""
    logging.basicConfig(format="%(asctime)-15s [%(levelname)s] %(message)s")
    if hasattr(args, "log_level"):
        log.setLevel(args.log_level)
    else:
        log.setLevel(logging.INFO)
