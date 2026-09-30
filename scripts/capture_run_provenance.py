"""Save Actions event provenance separately; missing API evidence blocks cutover."""
import argparse
import json
import os
from pathlib import Path

from sale_monitor.validation_integrity import capture_provenance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = capture_provenance(args.state, args.run_id, token=os.environ.get("GITHUB_TOKEN"))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
