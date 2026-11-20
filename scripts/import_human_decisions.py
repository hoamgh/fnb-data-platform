"""Import explicit human annotations, never AI suggestions."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fnb_platform.human_decisions import main

if __name__ == "__main__":
    main()
