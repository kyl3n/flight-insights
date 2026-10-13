"""Run a CI check and expose bounded failure details through GitHub annotations."""

from collections import deque
from pathlib import Path
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("Usage: ci-check.py LOG_PATH COMMAND [ARG ...]")
    log_path = Path(sys.argv[1])
    tail: deque[str] = deque(maxlen=300)
    with log_path.open("w", encoding="utf-8") as log:
        with subprocess.Popen(
            sys.argv[2:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
                tail.append(line)
            code = process.wait()
    if code:
        # GitHub truncates each annotation at about 4 KiB. A single large tail
        # lost the final exception behind early build output. Keep each UTF-8
        # chunk bounded and expose at most eight annotations.
        detail = "".join(tail).encode("utf-8")[-24000:]
        chunks = [detail[index:index + 3000].decode("utf-8", errors="replace")
                  for index in range(0, len(detail), 3000)] or ["Check failed without output."]
        for index, chunk in enumerate(chunks, 1):
            escaped = chunk.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Check failed ({log_path.name}, tail {index}/{len(chunks)})::{escaped}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
