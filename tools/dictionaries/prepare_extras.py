"""Prepare the pinned Extras sources."""
import argparse
from prepare import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    prepare('EXTRAS', args.output_dir)


if __name__ == '__main__':
    main()
