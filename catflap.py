#!/usr/bin/env python3
"""Root shim so `python catflap.py` still works after the package split.

The real code lives in the `catflap/` package; installs use the
`catflap = catflap.cli:main` console script.
"""
from catflap import main

if __name__ == "__main__":
    main()
