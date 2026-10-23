"""Archive monthly CityBikes events to MinIO Bronze and Silver."""
import argparse
from urbanhub_common.ops import start_run, finish_run
from .citybikes_archive import load_month

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd",choices=["archive"])
    parser.add_argument("--months",nargs="+",default=["202609"])
    args = parser.parse_args()
    run = start_run("mobility","citybikes-archive")
    try:
        frames = [frame for month in args.months for frame in load_month(month)]
        rows = sum(len(frame) for frame in frames)
        finish_run(run,"success",rows_received=rows,rows_inserted=rows,api_calls=len(frames))
    except Exception as exc:
        finish_run(run,"error",error=repr(exc)[:2000])
        raise

if __name__ == "__main__":
    main()
