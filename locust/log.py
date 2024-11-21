from __future__ import annotations

import atexit
import logging
import logging.config
import re
import socket
from collections import deque
from logging.handlers import QueueHandler, QueueListener
from time import time
from typing import Callable, cast

from gevent import Greenlet
from gevent.queue import Full, Queue

HOSTNAME = re.sub(r"\..*", "", socket.gethostname())

# Global flag that we set to True if any unhandled exception occurs in a greenlet
# Used by main.py to set the process return code to non-zero
unhandled_greenlet_exception = False

logger = logging.getLogger(__name__)


class LogReader(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.logs = deque(maxlen=500)

    def emit(self, record) -> None:
        self.logs.append(self.format(record))
        
        
class AutoStartQueueListener(QueueListener):
    def __init__(self, queue: Queue, *handlers: logging.Handler, respect_handler_level: bool = False) -> None:
        super().__init__(queue, *handlers, respect_handler_level=respect_handler_level)
        
        self.start()
        
        
class DiscardingQueueHandler(QueueHandler):
    def __init__(self, queue: Queue) -> None:
        super().__init__(queue)
        
        self._discarding_started: float | None = None
        self._discarding_count: int = 0
        
    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            super().enqueue(record)

            if self._discarding_started is not None:
                delta = time() - self._discarding_started
                logger.warning("Discarded %d log messages during %.2f seconds due to excessive logging resulting in log queue being full", self._discarding_count, delta)
                
            self._discarding_started = None
            self._discarding_count = 0
        except Full:
            if self._discarding_started is None:
                self._discarding_started = time()

            self._discarding_count += 1


def setup_logging(loglevel, logfile: str | None = None, maxsize: int = 10000) -> None:
    loglevel = loglevel.upper()

    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": f"[%(asctime)s] {HOSTNAME}/%(levelname)s/%(name)s: %(message)s",
            },
            "plain": {
                "format": "%(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            "console_plain": {
                "class": "logging.StreamHandler",
                "formatter": "plain",
            },
            "log_reader": {"class": "locust.log.LogReader", "formatter": "default"},
            "queue_listener": {
                "class": "locust.log.DiscardingQueueHandler",
                "listener": "locust.log.AutoStartQueueListener",
                "handlers": ["console", "log_reader"],
                "queue": {
                    "()": "gevent.queue.Queue",
                    "maxsize": maxsize,
                }
            },
            "queue_listener_plain": {
                "class": "locust.log.DiscardingQueueHandler",
                "listener": "locust.log.AutoStartQueueListener",
                "handlers": ["console_plain"],
                "queue": {
                    "()": "gevent.queue.Queue",
                    "maxsize": maxsize,
                }
            },
        },
        "loggers": {
            "locust": {
                "handlers": ["queue_listener"],
                "level": loglevel,
                "propagate": False,
            },
            "locust.stats_logger": {
                "handlers": ["queue_listener_plain"],
                "level": "INFO",
                "propagate": False,
            },
        },
        "root": {
            "handlers": ["queue_listener"],
            "level": loglevel,
        },
    }
    if logfile:
        # if a file has been specified add a file logging handler and set
        # the locust and root loggers to use it
        LOGGING_CONFIG["handlers"]["file"] = {
            "class": "logging.FileHandler",
            "filename": logfile,
            "formatter": "default",
        }
        LOGGING_CONFIG["loggers"]["locust"]["handlers"] = ["file", "queue_listener"]
        LOGGING_CONFIG["root"]["handlers"] = ["file", "queue_listener"]

    logging.config.dictConfig(LOGGING_CONFIG)


def get_logs() -> list[str]:
    log_reader_handler = [handler for handler in logging.getLogger("root").handlers if handler.name == "log_reader"]

    if log_reader_handler:
        return list(cast(LogReader, log_reader_handler[0]).logs)

    return []


def greenlet_exception_logger(logger: logging.Logger, level=logging.CRITICAL) -> Callable[[Greenlet], None]:
    """
    Return a function that can be used as argument to Greenlet.link_exception() that will log the
    unhandled exception to the given logger.
    """

    def exception_handler(greenlet: Greenlet) -> None:
        if greenlet.exc_info is not None and greenlet.exc_info[0] == SystemExit:
            logger.log(
                min(logging.INFO, level),  # dont use higher than INFO for this, because it sounds way to urgent
                "sys.exit(%s) called (use log level DEBUG for callstack)" % greenlet.exc_info[1],
            )
            logger.log(logging.DEBUG, "Unhandled exception in greenlet: %s", greenlet, exc_info=greenlet.exc_info)
        else:
            logger.log(level, "Unhandled exception in greenlet: %s", greenlet, exc_info=greenlet.exc_info)
        global unhandled_greenlet_exception
        unhandled_greenlet_exception = True

    return exception_handler
