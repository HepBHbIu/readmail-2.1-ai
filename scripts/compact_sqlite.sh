#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T readmail-new python - <<'PY'
from app.db import compact_db
print(compact_db())
PY
