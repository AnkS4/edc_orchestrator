# Data Orchestration Microservice Documentation

## 1. Project Overview

**Purpose**  
Asynchronous data transfer orchestration service implementing Dataspace Protocol standards. Manages full transfer lifecycle from initiation to final storage.

**Key Features**  
- EDC connector integration  
- Background processing threads  
- In-memory state tracking  
- API key authentication  
- Configurable retry logic  
- Downloaded data persistence  

**Technology Stack**

| Component   | Technology    | Version |  
|-------------|---------------|---------|  
| Framework   | Flask         | 2.3.3   |  
| RESTful     | Flask-RESTful | 0.3.10  |  
| Validation  | Marshmallow   | 4.0.0   |  
| HTTP Client | Requests      | 2.31.0  |  
| Config      | python-dotenv | 1.0.0   |  

---

## 2. System Architecture

```plaintext
[Client]  
   │ POST /orchestrate  
   └─> [Flask API]  
       │  
       ├─> [Transfer Process Resource]  
       │   │  
       │   └─> [Background Thread]  
       │       ├─> [EDC API Calls]  
       │       ├─> [Data Address Retrieval]  
       │       └─> [Data Download]  
       │  
       └─> [Storage Layer]  
           │  
           └─> [In-Memory Store]  
```

**Layer Breakdown**  
1. **API Layer**  
   - Entry point for client requests  
   - Authentication & validation  

2. **Processing Layer**  
   - Background thread management  
   - EDC API communication  
   - Error handling & retries  

3. **Storage Layer**  
   - In-memory orchestration state  
   - File persistence for downloaded data  


### Project Structure

```
/
├── app/                      # Core application package
│   ├── resources/           # API endpoint implementations
│   │   ├── transfer.py         🠞 Main orchestration logic (POST /orchestrate)
│   │   └── status.py           🠞 Status endpoints (GET /status, GET /status/{id})
│   │
│   ├── utils/               # Shared utilities
│   │   ├── error_handling.py   🠞 Consistent error response formatting
│   │   ├── helpers.py          🠞 HTTP request utilities & time formatting
│   │   └── storage.py          🠞 In-memory state management
│   │
│   ├── __init__.py             🠞 Application factory & logging setup
│   └─── config.py              🠞 Central configuration management
│
│── data/                    # Files saved after the download
│── docs/                    # API documentation files
│── logs/                    # Project logs
├── run.py                      🠞 Application entry point
├── requirements.txt            🠞 Python dependencies
└── README.md                # This documentation
 
```

---

## 3. Installation & Setup

**Prerequisites**  
- Python 3.8+  
- pip package manager  
- (Optional) Redis/PostgreSQL for production  

**Setup Steps**  
1. Clone Repository:  
   ```bash
   git clone https://github.com/yourusername/data-orchestrator.git
   cd data-orchestrator
   ```

2. Install Dependencies:  
   ```bash
   pip install -r requirements.txt
   ```

**Environment Variables**  

| Variable                   | Default    | Purpose                 |  
|----------------------------|------------|-------------------------|  
| `EDC_API_KEY`              | `password` | API authentication      |  
| `DATA_STORAGE_PATH`        | `./data`   | Downloaded file storage |  
| `REQUEST_TIMEOUT`          | `5`        | HTTP request timeout    |  
| `DATA_ADDRESS_MAX_RETRIES` | `3`        | EDR retrieval attempts  |  

---

## 4. Configuration

**Configuration Sources**  
1. **Environment Variables**  
   - `.env` file (highest priority)  
2. **Code Defaults**  
   - `config.py` (fallback values)  

**Key Settings**  

| Parameter         | Type | Description                                         |  
|-------------------|------|-----------------------------------------------------|  
| `LOG_LEVEL`       | str  | `DEBUG`, `INFO`, `WARNING`                          |  
| `CACHE_TTL`       | int  | Cache expiration (unused in current implementation) |  
| `STORAGE_API_URL` | str  | External storage endpoint (future extensibility)    |  

---

## 5. API Endpoints

### 5.1 POST /orchestrate
**Purpose**  
Initiate new data transfer process  

**Request Headers**  

| Header         | Required | Value              |  
|----------------|----------|--------------------|  
| `Content-Type` | Yes      | `application/json` |  
| `X-Api-Key`    | Yes      | Your API key       |  

**Request Body Schema**  
```json
{
"data": [
   {
    "type": "edc-asset",
    "counterPartyAddress": "http://provider-qna-controlplane:8082/api/dsp",
    "contractId": "2b9d3ab6-8325-4c0f-a1f9-56f0e103585e",
    "connectorId": "did:web:provider-identityhub:7083:provider"
  },
   {
    "type": "edc-asset",
    "counterPartyAddress": "http://provider-qna-controlplane:8082/api/dsp",
    "contractId": "2b9d3ab6-8325-4c0f-a1f9-56f0e103585e",
    "connectorId": "did:web:provider-identityhub:7083:provider"
  }
],
"connectorAddress": "http://x.x.x.x/consumer/cp"
}
```

**Response**  
```json
{
    "status": "SUCCESS",
    "status_code": 200,
    "orchestration_id": "c858791f-e6ac-43d0-bc03-4accd023b80a",
    "response": {
        "orchestration_id": "c858791f-e6ac-43d0-bc03-4accd023b80a",
        "status": "QUEUED",
        "message": "Transfer processing started"
    }
}
```

---

### 5.2 GET /status
**Purpose**  
Retrieve all active orchestration processes  

**Response Structure**  
```json
{
    "status": "SUCCESS",
    "status_code": 200,
    "response": {
        "orchestration_processes": {
            "85588705-c860-4348-a02b-fb8f36b9b219": {
                "orchestration_id": "85588705-c860-4348-a02b-fb8f36b9b219",
                "process_status": "DATA_ADDRESS_RETRIEVED",
                "created_at": "2025-05-22T08:37:13.433908+00:00",
                "updated_at": "2025-05-22T08:37:14.624743+00:00",
                "properties": {}
            }
        }
    }
}
```

---

### 5.3 GET /status/{orchestration_id}
**Purpose**  
Get detailed status for specific process  

**Path Parameters**

| Parameter          | Type | Required |  
|--------------------|------|----------|  
| `orchestration_id` | str  | Yes      |  

**Response**  
```json
{
    "status": "SUCCESS",
    "status_code": 200,
    "response": {
        "orchestration_processes": {
            "c858791f-e6ac-43d0-bc03-4accd023b80a": {
                "orchestration_id": "c858791f-e6ac-43d0-bc03-4accd023b80a",
                "process_status": "COMPLETED",
                "created_at": "2025-05-22T13:18:03.627937+00:00",
                "updated_at": "2025-05-22T13:18:12.874444+00:00",
                "storage_paths": [
                    {
                        "transfer_id": "f3a4b4e9-ccf7-4505-b1ea-50c282249d7a",
                        "storage_path": "./data/file_f3a4b4e9-ccf7-4505-b1ea-50c282249d7a_20250522151810_0b56e3.json",
                        "status": "SAVED"
                    },
                    {
                        "transfer_id": "c6e0e5c0-2001-4f22-a739-2df9c4a777cd",
                        "storage_path": "./data/file_c6e0e5c0-2001-4f22-a739-2df9c4a777cd_20250522151812_3e793f.json",
                        "status": "SAVED"
                    }
                ]
            }
        }
    }
}
```

---

## 6. Core Components

### 6.1 Transfer Process Resource
**Location**: `app/resources/transfer.py`  
**Key Features**  
- Background thread management  
- EDC API communication  
- Retry logic for EDR retrieval  
- Data persistence  

**Implementation Flow**  
1. Validate request  
2. Store in in-memory state  
3. Spawn background thread  
4. Initiate EDC transfer process  
5. Retrieve data address  
6. Download and save data

---

### 6.2 Error Handling
**Location**: `app/utils/error_handling.py`  

**Error Types**  

| Code | Type   | Description        |  
|------|--------|--------------------|  
| 400  | Client | Validation failure |  
| 403  | Client | Invalid API key    |  
| 500  | Server | Internal error     |  
| 502  | Server | Connection error   |  
| 504  | Server | Timeout            |  

**Response Format**
```json
{
  "message": "Invalid request format",
  "status": "ERROR",
  "status_code": 400,
  "details": {"data": ["Missing data for required field."]}
}
```

---

### 6.3 Storage Layer
**Location**: `app/utils/storage.py`  
**Implementation**  
- Thread-safe in-memory dictionary  
- Lock-based synchronization  
- Easy replacement for persistent storage  

---
