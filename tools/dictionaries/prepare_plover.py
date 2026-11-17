"""Prepare the pinned Plover image."""
import argparse
from prepare import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    prepare('PLOVER', args.output_dir)


if __name__ == '__main__':
    main()
