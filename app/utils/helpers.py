import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


def import_time():
    """Helper function to get current time in UTC with timezone info."""

    return datetime.now(timezone.utc).isoformat()


def make_request(method, url, **kwargs):
    """
    Unified request handler with timeout and error logging.

    Args:
        method: HTTP method (GET/POST)
        url: Target endpoint
        **kwargs: Additional request parameters

    Returns:
        requests.Response: Response object

    Raises:
        requests.exceptions.RequestException: On request failure
    """
    try:
        kwargs.setdefault('timeout', 3)  # Default timeout if not specified
        response = requests.request(method.lower(), url, **kwargs)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed to {url}: {str(e)}")
        raise e
