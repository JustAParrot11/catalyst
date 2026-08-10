"""python -m catalyst.dashboard  ->  the server on 0.0.0.0:8000."""

from catalyst.dashboard.server import main

if __name__ == "__main__":
    raise SystemExit(main())
