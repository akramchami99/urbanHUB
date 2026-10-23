"""Lake administration and file-based Gold jobs: python -m urbanhub_common."""
import argparse
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from .lake import lake_bucket, s3_client


def init_bucket():
    from botocore.exceptions import ClientError
    client, bucket = s3_client(), lake_bucket()
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        kwargs = {"Bucket": bucket}
        region = os.getenv("S3_REGION", "us-east-1")
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        client.create_bucket(**kwargs)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    print(f"MinIO bucket ready: s3://{bucket} (versioning enabled)")


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "check", "gold"])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start", help="UTC inclusive start (ISO date or timestamp)")
    parser.add_argument("--end", help="UTC exclusive end (ISO date or timestamp)")
    args = parser.parse_args()
    if args.command == "init":
        init_bucket()
    elif args.command == "check":
        client = s3_client()
        for zone in ("bronze", "silver", "gold", "metadata"):
            count = size = 0
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=lake_bucket(), Prefix=zone + "/"):
                for item in page.get("Contents", []):
                    count += 1
                    size += item["Size"]
            print(f"{zone}: {count} objects, {size} bytes")
    else:
        from .gold import publish
        if args.days <= 0:
            parser.error("--days must be positive")
        end = args.end or datetime.now(timezone.utc)
        start = args.start or (datetime.now(timezone.utc) - timedelta(days=args.days))
        frame, paths = publish(start, end)
        print(f"Gold: {len(frame)} city/hour rows, {len(paths)} Parquet objects")
        for path in paths:
            print(path)


if __name__ == "__main__":
    main()
