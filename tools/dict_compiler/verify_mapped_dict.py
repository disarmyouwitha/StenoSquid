"""Check every source outline against the shared compact-map reader."""
import argparse
import json
from pathlib import Path
from jsc4 import reader


def verify(source, blob, base, index=0, count=1):
    lookup = reader(blob, base, index, count)
    for outline, expected in source.items():
        actual = lookup(outline)
        assert actual == expected, (outline, expected, actual)
    return len(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument("--base-addr", type=lambda value: int(value, 0), required=True)
    parser.add_argument("--index", type=int, default=0,
                        help="Which dictionary of the image to check")
    parser.add_argument("--count", type=int, default=1,
                        help="How many dictionaries the image must hold")
    args = parser.parse_args()
    count = verify(json.loads(args.source.read_text(encoding="utf-8")), args.image.read_bytes(),
                   args.base_addr, args.index, args.count)
    print(f"PASS: {count:,} outlines match the mapped binary")


if __name__ == "__main__":
    main()
