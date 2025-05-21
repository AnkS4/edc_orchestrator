import logging
from functools import wraps

import requests
from flask import jsonify

logger = logging.getLogger(__name__)


def create_success_response(status_code=200, data=None, orchestration_id=None):
    """Creates a standardized success response with optional orchestration_id at the top level."""

    if orchestration_id is not None:
        response = {
            'status': 'SUCCESS',
            'status_code': status_code,
            'orchestration_id': orchestration_id,
            'response': data
        }

    else:
        response = {
            'status': 'SUCCESS',
            'status_code': status_code,
            'response': data
        }

    return jsonify(response)


def create_error_response(message, details=None, status_code=400):
    """Create error response with embedded status code"""
    response = jsonify({
        'message': message,
        'status': 'ERROR',
        'status_code': status_code,
        'details': details
    })
    response.status_code = status_code  # Set status code directly on response
    return response


def handle_exceptions(f):
    """
    Decorator to catch exceptions in API endpoints and return consistent JSON error responses.

    Catches common exceptions such as connection errors, timeouts, validation errors,
    and unexpected exceptions, logging details for debugging.

    Handles:
    - Timeouts (504)
    - Connection errors (502)
    - Validation errors (400)
    - Generic errors (500)
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)

        except requests.exceptions.Timeout as e:
            logger.error(f"Connection timeout: {e}")
            return create_error_response(
                "Connection to EDC Consumer API timed out",
                str(e),
                504
            )

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error: {e}")
            return create_error_response(
                "Failed to connect to EDC Consumer API",
                str(e),
                502
            )

        except ValueError as e:
            logger.error(f"Validation error: {e}")
            return create_error_response(
                str(e),
                None,
                400
            )

        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            return create_error_response(
                "An unexpected error occurred",
                str(e),
                500
            )
    return decorated_function
