"""Offline category quality CLI; no crawler or Gold publication."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fnb_platform.category_quality import main

if __name__ == "__main__":
    main()
