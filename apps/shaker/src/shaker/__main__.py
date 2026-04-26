from __future__ import annotations

import asyncio
import logging
import sys

from shaker import runtime

log = logging.getLogger("shaker")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    while True:
        code = asyncio.run(runtime.run())
        if code == runtime.EXIT_RESTART:
            log.info("restarting after config change")
            continue
        return code


if __name__ == "__main__":
    sys.exit(main())
