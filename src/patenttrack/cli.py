"""Single responsibility: command line entry point.

Defaults to the bundled fixture.  An optional path (or ``-`` for stdin) lets an
import tool feed a mixed batch without touching repository fixtures, which is
how out-of-order / partial imports and recovery re-runs are exercised.
"""

from __future__ import annotations

import json
import sys

from . import pipeline


def _load(path: str | None):
    if not path:
        return None
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    records = _load(args[0]) if args else None
    print(json.dumps(pipeline.run(records=records), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
