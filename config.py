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

# Qdrant (in-memory for full offline, or external server)
QDRANT_MODE = "memory"  # "memory" | "server"
QDRANT_URL = "http://localhost:6333"
QDRANT_COLLECTION = "qwe_qwe"

# SQLite
DB_PATH = "qwe_qwe.db"

# Agent
SYSTEM_PROMPT = """You are a helpful AI assistant with access to tools.
You can remember things, read/write files, and run shell commands.
Be concise. Use tools when needed, not for trivial questions.
When you learn something important about the user, save it to memory."""

MAX_HISTORY_MESSAGES = 6  # last N messages in context
MAX_MEMORY_RESULTS = 3    # top-K from Qdrant per query
MAX_TOOL_ROUNDS = 5       # max consecutive tool calls per turn
