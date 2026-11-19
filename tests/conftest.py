from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run


def pytest_configure(config):
    if not config.option.basetemp:
        import uuid
        root = Path(__file__).resolve().parent.parent / "tmp"
        root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(root / f"pytest-{uuid.uuid4().hex[:8]}")
