from __future__ import annotations

import multiprocessing


def main() -> None:
    multiprocessing.freeze_support()

    from omr_grader.bootstrap import run

    run()


if __name__ == "__main__":
    main()
