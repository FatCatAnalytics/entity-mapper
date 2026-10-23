"""Start Entity Mapper: API + built React UI on http://127.0.0.1:8765 (local only)."""
import os
import threading
import webbrowser

import uvicorn

PORT = int(os.environ.get("ENTITY_MAPPER_PORT", "8765"))

if __name__ == "__main__":
    if os.environ.get("ENTITY_MAPPER_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    uvicorn.run("api.server:app", host="127.0.0.1", port=PORT, log_level="info")
