from threading import Lock

"""
In-memory storage for orchestration processes.

Note: For production environments, replace with persistent storage
like Redis or PostgreSQL.
"""
orchestration_store = {}
orchestration_store_lock = Lock()

# Note: For production environments, consider replacing this with a persistent
# storage solution like Redis, MongoDB, or a relational database
