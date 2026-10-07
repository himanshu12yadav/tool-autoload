import sys


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        from autoreload import selftest

        sys.exit(selftest.run(sys.argv[2]))

    from autoreload.app import App

    App().mainloop()


if __name__ == "__main__":
    main()
