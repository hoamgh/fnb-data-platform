"""Offline M2 experimental ER runner."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fnb_platform.entity_resolution import main

if __name__ == "__main__":
    main()
