import multiprocessing

from netdoctor.cli.main import run

if __name__ == "__main__":
    multiprocessing.freeze_support()  # needed for the load-test process in frozen builds
    run()
