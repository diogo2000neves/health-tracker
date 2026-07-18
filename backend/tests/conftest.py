"""Make the repo root importable regardless of where pytest is invoked from."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
