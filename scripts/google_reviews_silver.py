"""Offline Google review-event to Silver transformation."""
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root/'src'),str(root)]
from fnb_platform.google_reviews_silver import main

if __name__=='__main__':main()
