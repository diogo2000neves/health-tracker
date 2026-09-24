"""Make the repo root importable regardless of where pytest is invoked from — and
the ingest directory too, because main.py imports its siblings flat (`import
workouts`), exactly as it runs in production (PYTHONPATH carries both). Without the
second entry test_ingest.py only passed when some earlier test module had happened
to add it."""
import pathlib
import sys

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "ingest"))
