import logging
import uuid
from typing import Any
import time
import requests
from flask import current_app, request
from flask_restful import Resource
from marshmallow import Schema, fields, ValidationError
from app.utils.storage import orchestration_store, orchestration_store_lock
from app.utils.helpers import import_time, make_request
from app.utils.error_handling import (
    handle_exceptions,
    create_error_response,
    create_success_response,
)

logger = logging.getLogger(__name__)


class DataEntrySchema(Schema):
    """Schema for validating data entry parameters."""
    endpoint = fields.Str(required=True)
    auth_type = fields.Str(
        load_default='none',
        validate=lambda x: x == 'none'
    )


class ServiceSchema(Schema):
    """Schema for validating service transfer parameters."""
    counterPartyAddress = fields.Str(required=True)
    contractId = fields.Str(required=True)
    connectorId = fields.Str(required=True)
    clientIp = fields.Str(required=True)


class CombinedTransferSchema(Schema):
    """Schema for validating combined transfer requests."""
    service = fields.Nested(ServiceSchema, required=True)
    data = fields.List(
        fields.Nested(DataEntrySchema),
        required=True,
        validate=lambda x: len(x) > 0
    )


class TransferProcessResource(Resource):
    """Resource for handling combined service/data transfers."""

    def __init__(self):
        """Initialize resource with configuration values."""
        self.timeout = current_app.config['REQUEST_TIMEOUT']
        self.api_key = current_app.config['EDC_API_KEY']
        self.headers = {
            'Content-Type': 'application/json',
            'X-Api-Key': self.api_key,
        }
        self.data_address_delay = current_app.config['DATA_ADDRESS_DELAY']
        self.data_address_max_retries = current_app.config['DATA_ADDRESS_MAX_RETRIES']
        self.schema = CombinedTransferSchema()

    def _update_orchestration_status(self, orchestration_id: str, status: str, **kwargs: Any):
        """
        Update orchestration process status in shared storage.

        Args:
            orchestration_id (str): Unique identifier for the orchestration
            status (str): New status value
            **kwargs: Additional fields to update
        """
        with orchestration_store_lock:
            process = orchestration_store.get(orchestration_id)
            if process:
                process.update({
                    'status': status,
                    'updated_at': import_time(),
                    **kwargs
                })

    @handle_exceptions
    def post(self):
        """Handle combined transfer request with data-first processing."""
        logger.info("Processing combined transfer request")

        api_key = request.headers.get('X-Api-Key')
        if not api_key:
            return create_error_response('Missing API key', 401)
        if api_key != self.api_key:
            return create_error_response('Invalid API key', 403)

        orchestration_id = str(uuid.uuid4())

        try:
            data = self.schema.load(request.get_json())

            with orchestration_store_lock:
                orchestration_store[orchestration_id] = {
                    'status': 'INITIALIZING',
                    'type': 'combined',
                    'original_request': data,
                    'created_at': import_time(),
                    'updated_at': import_time(),
                    'transfer_id': None,
                    'data_entries': []
                }

            data_responses = []
            for entry in data['data']:
                response = self._handle_data_registration(entry, orchestration_id)
                if response.status_code >= 400:
                    return response
                data_responses.append(response.get_json()['workflow'])

            service_response = self._handle_service_transfer(data['service'], orchestration_id)
            if service_response.status_code >= 400:
                return service_response

            self._update_orchestration_status(
                orchestration_id,
                'COMPLETED',
                service_response=service_response.get_json()['workflow'],
                data_responses=data_responses
            )

            return create_success_response({
                'orchestration_id': orchestration_id,
                'service': service_response.get_json()['workflow'],
                'data': data_responses,
                'status': 'COMPLETED'
            })

        except ValidationError as ve:
            logger.error(f"Validation error: {ve.messages}")
            return create_error_response(
                "Invalid request format",
                details=ve.messages,
                status_code=400
            )
        except Exception as exc:
            logger.error(f"Combined transfer failed: {str(exc)}", exc_info=True)
            self._update_orchestration_status(orchestration_id, 'FAILED', error=str(exc))
            return create_error_response(
                message=str(exc),
                status_code=500
            )

    def _handle_service_transfer(self, args: dict, orchestration_id: str):
        """Execute service transfer through EDC API."""
        logger.info("Initiating service transfer: %s", orchestration_id)

        transfer_request = {
            "@context": ["https://w3id.org/edc/connector/management/v0.0.1"],
            "counterPartyAddress": args['counterPartyAddress'],
            "contractId": args['contractId'],
            "connectorId": args['connectorId'],
            "dataDestination": {"type": "HttpProxy"},
            "protocol": "dataspace-protocol-http",
            "transferType": "HttpData-PULL"
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
                raise ValueError("Invalid EDC transfer response: missing '@id'")

            self._update_orchestration_status(
                orchestration_id,
                'INITIATED',
                transfer_id=transfer_id,
                edc_response=transfer_data,
            )

            data_address_response = self._retrieve_data_address(
                args['clientIp'],
                transfer_id,
                orchestration_id
            )
            if data_address_response.status_code >= 400:
                return data_address_response

            return create_success_response({
                'orchestration_id': orchestration_id,
                'transfer_id': transfer_id,
                'data_address': data_address_response.get_json()['workflow'],
                'status': 'DATA_ADDRESS_RETRIEVED',
            })

        except requests.HTTPError as exc:
            logger.error("EDC API error: %s", exc.response.text)
            self._update_orchestration_status(orchestration_id, 'FAILED', error=exc.response.text)
            return create_error_response(
                message="EDC API communication failed",
                details=exc.response.text,
                status_code=exc.response.status_code,
            )

    def _retrieve_data_address(self, client_ip: str, transfer_id: str, orchestration_id: str):
        """Retrieve data address from EDC with retry logic."""
        last_exception = None
        for attempt in range(self.data_address_max_retries):
            try:
                url = f"http://{client_ip}/consumer/cp/api/management/v3/edrs/{transfer_id}/dataaddress"
                if attempt > 0:
                    logger.info(f"Attempt {attempt + 1}: Adding delay")
                    time.sleep(self.data_address_delay)

                response = make_request('get', url, headers=self.headers, timeout=self.timeout)
                response.raise_for_status()

                self._update_orchestration_status(
                    orchestration_id,
                    'DATA_ADDRESS_RETRIEVED',
                    data_address=response.json()
                )
                return create_success_response(response.json())

            except Exception as exc:
                logger.error(f"Attempt {attempt + 1} failed: {exc}")
                last_exception = exc

        self._update_orchestration_status(orchestration_id, 'FAILED', error=str(last_exception))
        return create_error_response(
            message="Data address retrieval failed",
            details=str(last_exception),
            status_code=500,
        )

    def _handle_data_registration(self, args: dict, orchestration_id: str):
        """Register a data endpoint."""
        logger.info("Registering data endpoint: %s", orchestration_id)

        registration_data = {
            'endpoint': args['endpoint'],
            'auth_type': args.get('auth_type', 'none'),
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
