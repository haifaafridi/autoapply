"""
config.py — All settings live here.

Why a single config file?
  If you need to change a setting (like making the browser headless for speed),
  you change it in ONE place instead of hunting through multiple files.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load variables from your .env file into the environment
load_dotenv()

# ─── Paths ────────────────────────────────────────────────────────────────────

# __file__ is the path to this config.py file.
# .parent gives us the directory it's in — the project root.
BASE_DIR = Path(__file__).parent

# Where we save run logs. mkdir(exist_ok=True) means "create it if it doesn't exist,
# and don't error if it already does."
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# ─── API Keys ─────────────────────────────────────────────────────────────────

# os.getenv reads from the environment (which .env populates above).
# The second argument "" is the default value if the key isn't set.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─── Browser Settings ─────────────────────────────────────────────────────────

# headless=False means you can SEE the browser window.
# Set to True for faster/silent runs once you trust the tool.
BROWSER_HEADLESS = False

# slow_mo adds a delay (in milliseconds) between every Playwright action.
# 150ms makes the filling visible and human-like. Drop to 0 for speed.
BROWSER_SLOW_MO = 150

# How long (ms) to wait for a page element before giving up
DEFAULT_TIMEOUT_MS = 8000

# ─── Agent Settings ───────────────────────────────────────────────────────────

# How many times to retry the LLM if it returns malformed JSON (Phase 2)
MAX_LLM_RETRIES = 2

# Safety cap: stop processing after this many form pages
# (prevents infinite loops on weird sites)
MAX_PAGES = 10
EEO_LABELS = [
    "gender",
    "race",
    "ethnicity",
    "hispanic",
    "latino",
    "veteran",
    "disability",
    "national origin",
    "sexual orientation",
    "transgender",
]

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")