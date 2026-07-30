from __future__ import annotations

import multiprocessing


def main() -> None:
    multiprocessing.freeze_support()

    from omr_grader.startup import create_startup

    app, splash = create_startup()

    from omr_grader.bootstrap import run

    run(application=app, startup_splash=splash)


if __name__ == "__main__":
    main()
