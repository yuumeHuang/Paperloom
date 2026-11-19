"""Portable entry point; dependencies may also be installed in a normal venv."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
if (root / ".runtime").exists():
    sys.path.insert(0, str(root / ".runtime"))
sys.path.insert(0, str(root))

if __name__ == "__main__":
    from paperloom.cli import main
    main()
