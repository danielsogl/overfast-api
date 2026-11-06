#!/bin/bash
# Drop every rendered API response from Valkey, through the same
# ValkeyCache.evict_volatile_data() the app runs on startup and shutdown, so the
# key prefix and the key format cannot drift from what the app writes.
#
# Run by scripts/deploy.sh once app, worker and scheduler all run the new image.
# The app's own startup eviction happens earlier, while the old worker is still
# running: anything that worker renders after it (old code, old CSVs) would
# otherwise be served from Valkey for up to 24h. Prints the number of keys.
set -euo pipefail

docker compose exec -T app /code/.venv/bin/python -c '
import asyncio

from app.adapters.cache import ValkeyCache

asyncio.run(ValkeyCache().evict_volatile_data())
' 2>&1 | grep -E "Evicted [0-9]+ rendered-response keys" || {
    echo "evict-rendered-cache: no eviction count reported" >&2
    exit 1
}
