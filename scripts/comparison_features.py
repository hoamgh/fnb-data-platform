"""Run M1.5 offline comparison feature generation without installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fnb_platform.comparison_features import main

if __name__ == "__main__":
    main()
