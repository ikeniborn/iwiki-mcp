"""Reject missing, skipped, or unsuccessful PostgreSQL CI coverage."""
import sys
import xml.etree.ElementTree as ET


def main(path):
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        print("PostgreSQL CI: missing or invalid JUnit report", file=sys.stderr)
        return 1
    cases = list(root.iter("testcase"))
    postgres = [case for case in cases if case.get("classname", "").startswith(
        "tests.postgres."
    )]
    rejected = sum(1 for case in cases if any(
        case.find(tag) is not None for tag in ("skipped", "failure", "error")
    ))
    if not postgres or rejected:
        print(
            f"PostgreSQL CI: {len(postgres)} database-suite cases, "
            f"{rejected} skipped or unsuccessful cases; refusing success",
            file=sys.stderr,
        )
        return 1
    print(f"PostgreSQL CI: {len(postgres)} cases passed without skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
