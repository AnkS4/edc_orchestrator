import logging
import uuid
from typing import Any, List
import time
import requests
from flask import current_app, request
from flask_restful import Resource, reqparse
from app.utils.storage import orchestration_store, orchestration_store_lock
from app.utils.helpers import import_time, make_request
from app.utils.error_handling import (
    create_error_response,
    create_success_response,
    handle_exceptions,
)

logger = logging.getLogger(__name__)

# Combined Request Parser
combined_parser = reqparse.RequestParser()
combined_parser.add_argument(
    'service',
    type=dict,
    required=True,
    help="Service configuration required",
    location='json'
)
combined_parser.add_argument(
    'data',
    type=list,
    required=True,
    help="Data entries required",
    location='json'
)

"""
# Service Parser (without type/properties)
service_parser = reqparse.RequestParser()
service_parser.add_argument(
    'counterPartyAddress',
    type=str,
    required=True,
    help="Counter party address required",
    location=('service',)
)
service_parser.add_argument(
    'contractId',
    type=str,
    required=True,
    help="Contract agreement ID required",
    location=('service',)
)
service_parser.add_argument(
    'connectorId',
    type=str,
    required=True,
    help="Provider connector ID required",
    location=('service',)
)
service_parser.add_argument(
    'clientIp',
    type=str,
    required=True,
    help="Client IP address required",
    location=('service',)
)

# Data Parser (without type/properties)
data_parser = reqparse.RequestParser()
data_parser.add_argument(
    'endpoint',
    type=str,
    required=True,
    help="Endpoint required",
    location=('data',)
)
data_parser.add_argument(
    'auth_type',
    type=str,
    default='none',
    choices=['none'],
    help="Supported auth types: none",
    location=('data',)
)
"""

class TransferProcessResource(Resource):
    def __init__(self):
        self.timeout = current_app.config['REQUEST_TIMEOUT']
        self.api_key = current_app.config['EDC_API_KEY']
        self.headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.api_key,
        }
        self.data_address_delay = current_app.config['DATA_ADDRESS_DELAY']
        self.data_address_max_retries = current_app.config['DATA_ADDRESS_MAX_RETRIES']

    def _update_orchestration_status(self, orchestration_id: str, status: str, **kwargs: Any):
        with orchestration_store_lock:
            process = orchestration_store.get(orchestration_id)
            if process:
                update_data = {
                    'status': status,
                    'updated_at': import_time(),
                    **kwargs,
                }
                orchestration_store[orchestration_id].update(update_data)

    @handle_exceptions
    def post(self):
        logger.info("Processing combined transfer request")

        # API Key Validation
        api_key = request.headers.get('X-Api-Key')
        if not api_key:
            return create_error_response('Missing API key', 401)
        if api_key != self.api_key:
            return create_error_response('Invalid API key', 403)

        orchestration_id = str(uuid.uuid4())
        init_data = {
            'status': 'INITIALIZING',
            'type': 'combined',
            'original_request': request.get_json(),
            'created_at': import_time(),
            'updated_at': import_time(),
            'transfer_id': None,
            'data_entries': []
        }

        try:
            """
            args = combined_parser.parse_args()
            service_args = service_parser.parse_args(req=args['service'])
            data_entries = [data_parser.parse_args(req=entry) for entry in args['data']]
            """

            # Replace existing parsing code
            # With manual JSON validation:
            try:
                json_data = request.get_json()

                # Validate service section
                service_data = json_data.get('service', {})
                if not all(k in service_data for k in ['counterPartyAddress', 'contractId', 'connectorId', 'clientIp']):
                    raise ValueError("Missing required service fields")

                # Validate data entries
                data_entries = json_data.get('data', [])
                for entry in data_entries:
                    if 'endpoint' not in entry:
                        raise ValueError("Missing endpoint in data entry")

                service_args = {
                    'counterPartyAddress': service_data['counterPartyAddress'],
                    'contractId': service_data['contractId'],
                    'connectorId': service_data['connectorId'],
                    'clientIp': service_data['clientIp']
                }

            except Exception as e:
                logger.error(f"Validation error: {str(e)}")
                return create_error_response(f"Invalid request format: {str(e)}", status_code=400)

            with orchestration_store_lock:
                orchestration_store[orchestration_id] = init_data

            # Process service transfer
            service_result = self._handle_service_transfer(service_args, orchestration_id)
            if isinstance(service_result, tuple):
                return service_result

            # Process data registrations
            data_results = []
            for data_args in data_entries:
                data_result = self._handle_data_registration(data_args, orchestration_id)
                if isinstance(data_result, tuple):
                    return data_result
                data_results.append(data_result[0].get_json()['data'])

            self._update_orchestration_status(
                orchestration_id,
                'COMPLETED',
                service_response=service_result[0].get_json()['data'],
                data_responses=data_results
            )

            return create_success_response({
                'orchestration_id': orchestration_id,
                'service': service_result[0].get_json()['data'],
                'data': data_results,
                'status': 'COMPLETED'
            })

        except Exception as exc:
            logger.error(f"Combined transfer failed: {str(exc)}", exc_info=True)
            self._update_orchestration_status(orchestration_id, 'FAILED', error=str(exc))
            return create_error_response(
                message=str(exc),
                orchestration_id=orchestration_id,
                status_code=500
            )

    def _handle_service_transfer(self, args: dict, orchestration_id: str):
        """Execute service transfer (modified from original)"""
        logger.info("Initiating service transfer: %s", orchestration_id)
        transfer_request = {
            "@context": ["https://w3id.org/edc/connector/management/v0.0.1"],
            "counterPartyAddress": args['counterPartyAddress'],
            "contractId": args['contractId'],
            "connectorId": args['connectorId'],
            "dataDestination": {"type": "HttpProxy"},
            "protocol": "dataspace-protocol-http",
            "transferType": "HttpData-PULL",
        }

        try:
            edc_url = f"http://{args['clientIp']}/consumer/cp/api/management/v3/transferprocesses"
            response = make_request(
                'post',
                edc_url,
                json=transfer_request,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            transfer_data = response.json()
            transfer_id = transfer_data.get('@id')

            if not transfer_id:
                raise ValueError("Invalid EDC transfer response")

            self._update_orchestration_status(
                orchestration_id,
                'INITIATED',
                transfer_id=transfer_id,
                edc_response=transfer_data,
            )

            data_address_result = self._retrieve_data_address(args['clientIp'], transfer_id, orchestration_id)
            if isinstance(data_address_result, tuple):
                return data_address_result

            return create_success_response({
                'orchestration_id': orchestration_id,
                'transfer_id': transfer_id,
                'data_address': data_address_result,
                'status': 'DATA_ADDRESS_RETRIEVED',
            })

        except requests.HTTPError as exc:
            logger.error("EDC API error: %s", exc.response.text)
            self._update_orchestration_status(orchestration_id, 'FAILED', error=exc.response.text)
            return create_error_response(
                message="EDC API communication failed",
                details=exc.response.text,
                orchestration_id=orchestration_id,
                status_code=exc.response.status_code,
            )

    # Keep existing _retrieve_data_address implementation
    def _retrieve_data_address(self, client_ip: str, transfer_id: str, orchestration_id: str):
        """
        Retrieve data address synchronously.

        Args:
            transfer_id: EDC transfer identifier
            orchestration_id: Orchestration process identifier

        Returns:
            Data address dict or error response tuple
        """
        last_exception = None
        for attempt in range(self.data_address_max_retries):
            try:
                url = f"http://{client_ip}/consumer/cp/api/management/v3/edrs/{transfer_id}/dataaddress"

                if attempt > 0:
                    logger.info(
                        f"Attempt {attempt + 1}: Adding {self.data_address_delay}s delay for EDC to assign the data address")
                    time.sleep(self.data_address_delay)

                logger.info(f"Attempt {attempt + 1}: Sent GET request to {url}")
                response = make_request(
                    'get',
                    url,
                    headers=self.headers,
                    timeout=self.timeout,
                )
                logger.info(f"Received response from {url}: {response.status_code} {response.text}")

                response.raise_for_status()

                data_address = response.json()

                with orchestration_store_lock:
                    process = orchestration_store.get(orchestration_id)
                    if process:
                        process.update({
                            'status': 'DATA_ADDRESS_RETRIEVED',
                            'data_address': data_address,
                            'updated_at': import_time(),
                        })
                return data_address

            except Exception as exc:
                logger.error("Attempt %d: Data address retrieval failed: %s", attempt + 1, str(exc))
                last_exception = exc
                # Only update status to 'FAILED' after final attempt

        # All retries failed
        self._update_orchestration_status(
            orchestration_id,
            'FAILED',
            error=str(last_exception),
        )
        return create_error_response(
            message="Data address retrieval failed",
            details=str(last_exception),
            orchestration_id=orchestration_id,
            status_code=500,
        )

    def _handle_data_registration(
        self,
        args: dict,
        orchestration_id: str
    ):
        """
        Handle data endpoint registration.

        Args:
            args: Validated request arguments
            orchestration_id: Process identifier

        Returns:
            Tuple containing (response, status_code)
        """
        logger.info("Registering data endpoint: %s", orchestration_id)
        registration_data = {
            'endpoint': args['endpoint'],
            'auth_type': args['auth_type'],
        }

        self._update_orchestration_status(
            orchestration_id,
            'REGISTERED',
            **registration_data,
        )

        return create_success_response({
            'orchestration_id': orchestration_id,
            **registration_data,
            'status': 'REGISTERED',
        })
