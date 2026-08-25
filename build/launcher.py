"""Frozen-app entry point."""

import multiprocessing
import sys

from redactor.gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main() or 0)
