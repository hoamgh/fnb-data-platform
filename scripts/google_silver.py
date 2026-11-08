"""Run the offline Silver staging CLI from this worktree without installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fnb_platform.google_silver import main

if __name__ == "__main__":
    main()
