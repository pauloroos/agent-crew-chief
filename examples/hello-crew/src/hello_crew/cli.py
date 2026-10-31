import sys

from hello_crew.greet import greet


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    print(greet(args[0] if args else "world"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
