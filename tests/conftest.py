import os
import sys
import tempfile
from pathlib import Path

# Isolate all state (SQLite, captures, undo bin) before any app module is imported.
os.environ["CASE_MC_HOME"] = tempfile.mkdtemp(prefix="case-mc-tests-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
