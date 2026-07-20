"""
Shared pytest setup.

Some modules under test import app.db.database at module load, which reads
DATABASE_URL from the environment and creates a SQLAlchemy engine. Point
that at a throwaway in-memory SQLite so tests don't need a real DB.

We must set the env var BEFORE any test-collection-time import touches
app.db.database — hence the direct os.environ mutation at file top.
"""

import os
import sys
import pathlib

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Make the repo root importable so `from app.pipeline...` works when pytest
# is invoked from anywhere.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
