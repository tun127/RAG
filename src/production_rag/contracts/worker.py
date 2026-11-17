"""One short-lived parser process; no services, model, database or retrieval imports."""

import sys
from pathlib import Path

from production_rag.contracts.models import ParseLimits
from production_rag.contracts.parser import parse_document


def main():
    source, output, version, limits_json = sys.argv[1:]
    limits = ParseLimits.model_validate_json(limits_json)
    with Path(source).open("rb") as stream:
        content = stream.read(limits.max_bytes + 1)
    result = parse_document(content, version, limits)
    Path(output).write_bytes(result.model_dump_json().encode("utf-8"))


if __name__ == "__main__":
    main()
