import logging

from app.core.logging import suppress_sensitive_http_client_logs


def test_sensitive_http_client_info_logs_are_suppressed() -> None:
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_levels = (httpx_logger.level, httpcore_logger.level)
    try:
        httpx_logger.setLevel(logging.INFO)
        httpcore_logger.setLevel(logging.INFO)

        suppress_sensitive_http_client_logs()

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(original_levels[0])
        httpcore_logger.setLevel(original_levels[1])
