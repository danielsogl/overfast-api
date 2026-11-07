"""The taskiq broker shared by the API process, the worker and the scheduler.

Its tasks are defined in ``app.worker``. The broker lives here, on its own, so
the API's lifespan can check ``broker.is_worker_process`` and start the broker
without importing the task module.
"""

from app.adapters.tasks.valkey_broker import ValkeyListBroker
from app.config import settings

broker = ValkeyListBroker(
    url=f"valkey://{settings.valkey_host}:{settings.valkey_port}",
    queue_name="taskiq:queue",
    max_pool_size=settings.worker_max_concurrent_jobs,
)
