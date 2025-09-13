import json
from datetime import datetime, timezone

# -----------------------
# Утилиты
# -----------------------
def now_iso():
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def load_config(path="config.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)