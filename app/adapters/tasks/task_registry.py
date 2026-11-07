"""Task registry — shared TASK_MAP populated by app/worker.py on import.

Keeping this in a separate module breaks the circular import chain:
  app/worker.py → dependencies.py → valkey_task_queue.py → task_registry.py
                                                             (no dep on worker)

``app.main`` imports ``app.worker`` so the API process, which enqueues, has the
map filled as well.
"""

from typing import Any

# Populated by app/worker.py once the broker.task-decorated functions are defined.
TASK_MAP: dict[str, Any] = {}
