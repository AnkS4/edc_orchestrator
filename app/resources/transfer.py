import logging
import uuid
from typing import Any
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

# Request Parsers
service_parser = reqparse.RequestParser()
service_parser.add_argument(
    'type',
    type=str,
    required=True,
    choices=['service'],
    help="Transfer type must be 'service'",
)
service_parser.add_argument(
    'contractId',
    type=str,
    required=True,
    help="Contract agreement ID required for service transfer",
)
service_parser.add_argument(
    'counterPartyAddress',
    type=str,
    required=True,
    help="Counter party address required for service transfer",
)
service_parser.add_argument(
    'connectorId',
    type=str,
    required=True,
    help="Provider connector ID",
)
service_parser.add_argument(
    'clientIp',
    type=str,
    required=True,
    help="Client IP address must be provided"
)
service_parser.add_argument(
    'properties',
    type=dict
)

data_parser = reqparse.RequestParser()
data_parser.add_argument(
    'type',
    type=str,
    required=True,
    choices=['data'],
    help="Transfer type must be 'data'",
)
data_parser.add_argument(
    'endpoint',
    type=str,
    required=True,
    help="Endpoint required for data registration",
)
data_parser.add_argument(
    'auth_type',
    type=str,
    default='none',
    choices=['none'],
    help="Supported auth type: none (default: none)",
)
data_parser.add_argument(
    'properties',
    type=dict
)


class TransferProcessResource(Resource):
    """
    Resource for managing service transfers and data registrations.

    Attributes:
        timeout (int): Request timeout from config
        api_key (str): EDC API key from config
        headers (dict): Preconfigured request headers
    """

    def __init__(self):
        """Initialize resource with validated configuration."""
        self.timeout = current_app.config['REQUEST_TIMEOUT']
        self.api_key = current_app.config['EDC_API_KEY']
        self.headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.api_key,
        }
        self.data_address_delay = current_app.config['DATA_ADDRESS_DELAY']
        self.data_address_max_retries = current_app.config['DATA_ADDRESS_MAX_RETRIES']

    def _update_orchestration_status(
        self,
        orchestration_id: str,
        status: str,
        **kwargs: Any
    ):
        """
        Update orchestration process status atomically.

        Args:
            orchestration_id: Unique process identifier
            status: New status value
            kwargs: Additional fields to update
        """
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
        """
        Main entry point for transfer requests.

        Returns:
            Tuple containing (response, status_code)
        """
        logger.info("Processing transfer request")

        # API Key Validation
        api_key = request.headers.get('X-Api-Key')
        if not api_key:
            return create_error_response('Missing API key', 401)
        if api_key != self.api_key:
            return create_error_response('Invalid API key', 403)

        orchestration_id = str(uuid.uuid4())
        init_data = {
            'status': 'INITIALIZING',
            'type': None,
            'original_request': request.get_json(),
            'created_at': import_time(),
            'updated_at': import_time(),
            'transfer_id': None,
        }

        try:
            request_data = request.get_json()
            if not request_data:
                return create_error_response('Invalid JSON body', 400)

            transfer_type = request_data.get('type')
            if transfer_type not in {'service', 'data'}:
                return create_error_response('Invalid transfer type', 400)

            init_data['type'] = transfer_type
            with orchestration_store_lock:
                orchestration_store[orchestration_id] = init_data

            if transfer_type == 'service':
                args = service_parser.parse_args()
                return self._handle_service_transfer(args, orchestration_id)
            else:
                args = data_parser.parse_args()
                return self._handle_data_registration(args, orchestration_id)

        except Exception as exc:
            logger.error(f"Transfer failed: {str(exc)}", exc_info=True)
            self._update_orchestration_status(
                orchestration_id,
                'FAILED',
                error=str(exc)
            )
            return create_error_response(
                message=str(exc),
                orchestration_id=orchestration_id,
                status_code=500
            )

    def _handle_service_transfer(
        self,
        args: dict,
        orchestration_id: str
    ):
        """
        Execute service transfer workflow.

        Args:
            args: Validated request arguments
            orchestration_id: Process identifier

        Returns:
            Tuple containing (response, status_code)
        """
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

        if args.get('properties'):
            transfer_request['properties'] = args['properties']

        # print("Transfer Request: ", transfer_request)

        client_ip = args.get('clientIp')
        edc_url = f"http://{client_ip}/consumer/cp/api/management/v3/transferprocesses"

        try:
            logger.info(f"Sent POST request to {edc_url} with payload: {transfer_request}")

            response = make_request(
                'post',
                edc_url,
                json=transfer_request,
                headers=self.headers,
                timeout=self.timeout,
            )

            logger.info(f"Received response from {edc_url}: {response.status_code} {response.text}")

            response.raise_for_status()
            transfer_data = response.json()
            transfer_id = transfer_data.get('@id')
            if not transfer_id:
                logger.error("EDC response missing transfer ID: %s", transfer_data)
                raise ValueError("Invalid EDC transfer response")

            self._update_orchestration_status(
                orchestration_id,
                'INITIATED',
                transfer_id=transfer_id,
                edc_response=transfer_data,
            )

            # Synchronously retrieve data address and check for errors
            data_address_result = self._retrieve_data_address(client_ip, transfer_id, orchestration_id)
            if isinstance(data_address_result, tuple):  # Indicates error response
                return data_address_result

            return create_success_response({
                'orchestration_id': orchestration_id,
                'transfer_id': transfer_id,
                'data_address': data_address_result,
                'status': 'DATA_ADDRESS_RETRIEVED',
            })

        except requests.HTTPError as exc:
            logger.error("EDC API error: %s", exc.response.text)
            self._update_orchestration_status(
                orchestration_id,
                'FAILED',
                error=exc.response.text,
            )
            return create_error_response(
                message="EDC API communication failed",
                details=exc.response.text,
                orchestration_id=orchestration_id,
                status_code=exc.response.status_code,
            )

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
            'properties': args.get('properties', {}),
        }

        self._update_orchestration_status(
            orchestration_id,
            'REGISTERED',
            **registration_data,
        )
        logger.info(f"Registered data endpoint for orchestration_id={orchestration_id}: {registration_data}")

        return create_success_response({
            'orchestration_id': orchestration_id,
            **registration_data,
            'status': 'REGISTERED',
        })
