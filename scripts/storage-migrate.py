#!/usr/bin/env python3
"""Copy every document from the S3/MinIO bucket to the local document folder.

For an install that wants to stop depending on the bundled MinIO (its images
left Docker Hub around 2026-09-11) and keep its documents on local disk.

    cd <install dir>
    .venv/bin/python scripts/storage-migrate.py            # copy, then verify
    .venv/bin/python scripts/storage-migrate.py --verify   # compare only, copy nothing
    .venv/bin/python scripts/storage-migrate.py --dest /srv/velaris/documents

What it does
------------
Reads the bucket named by VELARIS_CASE_S3_* (or the older VELARIS_CASE_MINIO_*)
settings in .env and writes each object,
byte for byte, under VELARIS_CASE_STORAGE_LOCAL_PATH. Objects are copied as
stored: when VELARIS_CASE_STORAGE_MASTER_KEY is set they are ciphertext in the
bucket and the same ciphertext on disk, readable with the same key. Nothing is
decrypted on the way.

What it does NOT do
-------------------
- It never deletes or changes anything in the bucket.
- It never edits .env and never restarts a service. Switching is the operator's
  step, printed at the end, and it is reversible until the bucket is removed.

Safe to re-run: an object already on disk at the same size is skipped. Each file
is written beside its final name and renamed, so an interrupted run leaves no
half-written document.

Exit status: 0 = every object in the bucket is on disk at the same size;
1 = something is missing or differs (listed); 2 = could not start.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fail(msg: str) -> "None":
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", help="local folder to copy into (default: VELARIS_CASE_STORAGE_LOCAL_PATH)")
    ap.add_argument("--verify", action="store_true", help="compare bucket and folder; copy nothing")
    args = ap.parse_args()

    # `.env` is resolved against the working directory, as for the services.
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / "services" / "case-service"))
    try:
        from velaris_shared.config import get_settings
        from minio import Minio
    except ImportError as exc:
        _fail(f"run this with the install's own interpreter (.venv/bin/python): {exc}")
    s = get_settings()

    dest = Path(args.dest or s.storage_local_path)
    if not dest.is_absolute():
        # A relative path means "relative to wherever the service was started",
        # which this script cannot know. Guessing would copy everything to a
        # folder the service then never reads.
        _fail(f"the document folder '{dest}' is relative. Pass --dest with the absolute "
              "path, and set VELARIS_CASE_STORAGE_LOCAL_PATH to the same path.")

    try:
        client = Minio(s.s3_endpoint, access_key=s.s3_access_key,
                       secret_key=s.s3_secret_key, secure=s.s3_secure)
        if not client.bucket_exists(s.s3_bucket):
            _fail(f"bucket '{s.s3_bucket}' does not exist at {s.s3_endpoint}")
        objects = [o for o in client.list_objects(s.s3_bucket, recursive=True) if not o.is_dir]
    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"cannot read bucket '{s.s3_bucket}' at {s.s3_endpoint}: {exc}")

    print(f"Bucket : {s.s3_endpoint}/{s.s3_bucket} — {len(objects)} object(s), "
          f"{sum(o.size or 0 for o in objects) / 1_048_576:.1f} MB")
    print(f"Folder : {dest}")
    if not args.verify:
        if not dest.exists():
            # Created here = private to this user. An existing folder keeps
            # whatever permissions its operator chose.
            dest.mkdir(parents=True, mode=0o700)
            dest.chmod(0o700)
        if not os.access(dest, os.W_OK):
            _fail(f"{dest} is not writable by this user")

    copied = skipped = 0
    problems: list[str] = []
    for o in objects:
        key = o.object_name
        if ".." in Path(key).parts or Path(key).is_absolute():
            problems.append(f"{key}: not a key the local store accepts")
            continue
        target = dest / key
        if target.is_file() and target.stat().st_size == o.size:
            skipped += 1
            continue
        if args.verify:
            problems.append(f"{key}: " + ("size differs" if target.exists() else "missing on disk"))
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            resp = client.get_object(s.s3_bucket, key)
            try:
                with open(tmp, "wb") as fh:
                    for chunk in resp.stream(1024 * 1024):
                        fh.write(chunk)
            finally:
                resp.close()
                resp.release_conn()
            if tmp.stat().st_size != o.size:
                tmp.unlink(missing_ok=True)
                problems.append(f"{key}: copied size differs from the bucket's")
                continue
            os.replace(tmp, target)
            copied += 1
        except Exception as exc:  # one bad object must not hide the rest
            problems.append(f"{key}: {exc}")

    print(f"Result : {copied} copied, {skipped} already on disk, {len(problems)} problem(s)")
    if problems:
        for p in problems[:50]:
            print(f"  ✗ {p}")
        if len(problems) > 50:
            print(f"  … and {len(problems) - 50} more")
        print("\nNOT complete — do not switch the storage setting yet.")
        return 1

    print("\n✓ Every object in the bucket is on disk at the same size.")
    if not args.verify:
        print(
            "\nTo switch (your step — nothing was changed):\n"
            "  1. In .env:  VELARIS_CASE_STORAGE_BACKEND=local\n"
            f"               VELARIS_CASE_STORAGE_LOCAL_PATH={dest}\n"
            "     (if secrets are rendered from OpenBao, change them there too)\n"
            "  2. Restart the services, open a document that existed before.\n"
            "  3. Re-run this with --verify if anything was uploaded in between.\n"
            "  4. Only when satisfied: VELARIS_BUNDLED_MINIO=0 in .env stops the\n"
            "     MinIO container being started. Its volume is left for you to remove."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
