import logging


def suppress_sensitive_http_client_logs() -> None:
    """Prevent query-string credentials from appearing in INFO request logs."""

    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
