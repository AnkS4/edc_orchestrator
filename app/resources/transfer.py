import logging
import uuid
from typing import Any
import time
import requests
from flask import current_app, request
from flask_restful import Resource
from marshmallow import Schema, fields, ValidationError, validates_schema
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
    type = fields.Str(required=True, validate=lambda x: x in ["edc-asset"])
    counterPartyAddress = fields.Str(required=True)
    contractId = fields.Str(required=True)
    connectorId = fields.Str(required=True)

    @validates_schema
    def validate_type_fields(self, data, **kwargs):
        if data['type'] == "edc-asset":
            required = ['counterPartyAddress', 'contractId', 'connectorId']
            for field in required:
                if field not in data:
                    raise ValidationError(f"{field} is required for edc-asset type")


class ServiceSchema(Schema):
    """Schema for validating service transfer parameters."""
    counterPartyAddress = fields.Str(required=True)
    contractId = fields.Str(required=True)
    connectorId = fields.Str(required=True)


class CombinedTransferSchema(Schema):
    """Schema for validating combined transfer requests."""
    service = fields.Nested(ServiceSchema, required=True)
    data = fields.List(
        fields.Nested(DataEntrySchema),
        required=True,
        validate=lambda x: len(x) > 0
    )
    connectorAddress = fields.Str(required=True)


class TransferProcessResource(Resource):
    """Resource for handling combined service/data transfers."""

    def __init__(self):
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
        logger.info("Processing combined transfer request")

        api_key = request.headers.get('X-Api-Key')
        if not api_key:
            return create_error_response('Missing API key', 401)
        if api_key != self.api_key:
            return create_error_response('Invalid API key', 403)

        orchestration_id = str(uuid.uuid4())

        try:
            data = self.schema.load(request.get_json())
            connector_address = data['connectorAddress']

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

            # Process data entries
            data_responses = []
            for entry in data['data']:
                response = self._handle_edc_request(
                    {**entry, 'type': 'edc-asset'},
                    edc_url=f"{connector_address}/api/management/v3/transferprocesses",
                    orchestration_id=orchestration_id,
                    transfer_type="HttpData-PULL",
                    success_status="ASSET_REGISTERED"
                )
                if response.status_code >= 400:
                    return response
                data_responses.append(response.get_json()['workflow'])

            # Process service transfer
            service_response = self._handle_edc_request(
                data['service'],
                edc_url=f"{connector_address}/api/management/v3/transferprocesses",
                orchestration_id=orchestration_id,
                transfer_type="HttpData-PULL",
                success_status="SERVICE_INITIATED"
            )
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
            return create_error_response("Invalid request format", details=ve.messages, status_code=400)
        except Exception as exc:
            logger.error(f"Combined transfer failed: {str(exc)}", exc_info=True)
            self._update_orchestration_status(orchestration_id, 'FAILED', error=str(exc))
            return create_error_response(str(exc), status_code=500)

    def _handle_edc_request(self, args: dict, edc_url: str, orchestration_id: str,
                            transfer_type: str, success_status: str):
        """Unified handler for EDC API requests."""
        logger.info(f"Processing EDC request to {edc_url}")

        request_data = {
            "@context": ["https://w3id.org/edc/connector/management/v0.0.1"],
            "counterPartyAddress": args['counterPartyAddress'],
            "contractId": args['contractId'],
            "connectorId": args['connectorId'],
            "protocol": "dataspace-protocol-http"
        }

        if 'transferType' in args:
            request_data["transferType"] = args['transferType']
        else:
            request_data["transferType"] = transfer_type

        """
        if args.get('type') == 'edc-asset':
            request_data['type'] = 'edc-asset'
        """

        print(f"Request data: {request_data}")

        try:
            response = make_request(
                'post',
                edc_url,
                json=request_data,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

            response_data = response.json()
            resource_id = response_data.get('@id')

            if not resource_id:
                raise ValueError("Invalid EDC response: missing resource ID")

            self._update_orchestration_status(
                orchestration_id,
                success_status,
                resource_id=resource_id,
                edc_response=response_data,
            )

            return create_success_response({
                'orchestration_id': orchestration_id,
                'resource_id': resource_id,
                'status': success_status,
                'type': args.get('type', 'service')
            })

        except requests.HTTPError as exc:
            logger.error(f"EDC API error ({edc_url}): {exc.response.text}")
            self._update_orchestration_status(orchestration_id, 'FAILED', error=exc.response.text)
            return create_error_response(
                "EDC API communication failed",
                details=exc.response.text,
                status_code=exc.response.status_code,
            )

    def _retrieve_data_address(self, connector_address: str, transfer_id: str, orchestration_id: str):
        last_exception = None
        for attempt in range(self.data_address_max_retries):
            try:
                url = f"{connector_address}/api/management/v3/edrs/{transfer_id}/dataaddress"
                if attempt > 0:
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
            "Data address retrieval failed",
            details=str(last_exception),
            status_code=500
        )
