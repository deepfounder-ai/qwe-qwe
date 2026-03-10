"""qwe-qwe configuration — all settings in one place."""

# LLM
LLM_BASE_URL = "http://192.168.0.49:1234/v1"
LLM_MODEL = "qwen/qwen3.5-9b"
LLM_API_KEY = "lm-studio"

# Embeddings (same LM Studio server)
EMBED_BASE_URL = "http://192.168.0.49:1234/v1"
EMBED_MODEL = "text-embedding-nomic-embed-text-v1.5"
EMBED_API_KEY = "lm-studio"
EMBED_DIM = 768

# Qdrant (local disk for persistence, no server needed)
QDRANT_MODE = "disk"  # "memory" | "disk" | "server"
QDRANT_PATH = "./memory"  # for disk mode
QDRANT_URL = "http://localhost:6333"  # for server mode
QDRANT_COLLECTION = "qwe_qwe"

# SQLite
DB_PATH = "qwe_qwe.db"

# Agent
MAX_HISTORY_MESSAGES = 6  # last N messages in context
MAX_MEMORY_RESULTS = 3    # top-K auto-retrieved from Qdrant per turn
MAX_TOOL_ROUNDS = 5       # max consecutive tool calls per turn
