import logging
import time

from flask import current_app
from flask_restful import Resource, reqparse

from app.utils.error_handling import create_error_response, create_success_response, handle_exceptions
from app.utils.helpers import make_request
from app.utils.storage import orchestration_store, orchestration_store_lock

logger = logging.getLogger(__name__)

detail_parser = reqparse.RequestParser()
detail_parser.add_argument(
    'clientIp',
    type=str,
    required=True,
    help="Client IP address must be provided"
)


def get_detailed_orchestration_data(orchestration_id, process):
    """
    Helper function to get detailed orchestration data for a given process.

    Args:
        orchestration_id (str): The orchestration ID
        process (dict): The orchestration process data

    Returns:
        dict: Detailed orchestration data
    """
    response_data = {
        'orchestration_id': orchestration_id,
        'process_status': process['status'],
        'created_at': process['created_at'],
        'updated_at': process['updated_at'],
        'properties': process.get('properties', {})
    }

    return response_data


class OrchestrationStatusResource(Resource):
    """Resource for retrieving status of all orchestration processes."""

    @handle_exceptions
    def get(self):
        """Get status of all orchestration processes with detailed information."""
        try:
            with orchestration_store_lock:
                # Create detailed data for each orchestration process
                detailed_processes = {}
                for orch_id, process in orchestration_store.items():
                    detailed_processes[orch_id] = get_detailed_orchestration_data(orch_id, process)

                return create_success_response(data={
                    'orchestration_processes': detailed_processes
                })
        except Exception as e:
            logger.error(f"Error retrieving all orchestration statuses: {str(e)}")
            return create_error_response(
                'Error retrieving orchestration statuses',
                str(e),
                status_code=500
            )


class OrchestrationDetailResource(Resource):
    """Simplified resource for single orchestration status details"""

    @handle_exceptions
    def get(self, orchestration_id):
        """Get status of a specific orchestration process without EDC calls"""
        with orchestration_store_lock:
            if orchestration_id not in orchestration_store:
                return create_error_response(
                    'Orchestration process not found',
                    f"ID '{orchestration_id}' does not exist",
                    404
                )

            process = orchestration_store[orchestration_id]
            response_data = self._get_basic_details(orchestration_id, process)

            return create_success_response(data={
                'orchestration_processes': {
                    orchestration_id: response_data
                }
            })

    def _get_basic_details(self, orch_id, process):
        """Return core process details from storage"""
        return {
            'orchestration_id': orch_id,
            'process_status': process['status'],
            'created_at': process['created_at'],
            'updated_at': process['updated_at'],
            'storage_paths': process.get('data_responses', [])
        }

