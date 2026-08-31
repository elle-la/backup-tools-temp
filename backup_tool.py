#!/usr/bin/env python3
"""
backup-tool: a tiny CLI that archives a data directory, on demand or on a
watch loop, using config sourced from env vars and/or a mounted config
file, and (optionally) encrypts archives using a passphrase sourced from
a mounted secret file.

Commands:
    backup-tool run [DIR]                 One-shot: archive DIR right now
    backup-tool watch [DIR]               Poll DIR every POLL_SECONDS and
                                           archive whenever something changes
    backup-tool list                      List archives currently in BACKUP_DIR
    backup-tool restore <archive> [DIR]   Extract an archive back into DIR

    DIR is optional on run/watch/restore - if omitted, it falls back to the
    DATA_DIR env var, then to /data. Supplying it directly means a single
    image can archive whatever directory you point it at, without needing
    a rebuild or an env var change per use.

Configuration sources (env vars win for the settings they provide;
the config file supplies settings that aren't exposed as env vars):
    RETENTION_DAYS   env var, default 7      - delete archives older than this
    BACKUP_PREFIX    env var, default "backup"
    POLL_SECONDS     env var, default 5      - watch-mode poll interval
    CONFIG_FILE      file, default /etc/backup-tool/backup.conf
                     key=value lines, e.g. COMPRESSION_LEVEL=6

Secret:
    PASSPHRASE_FILE  file, default /etc/backup-tool/secrets/passphrase
                     if present, archives are encrypted with `gpg`
                     (symmetric/passphrase mode) and restore requires it.
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/etc/backup-tool/backup.conf")
PASSPHRASE_FILE = os.environ.get(
    "PASSPHRASE_FILE", "/etc/backup-tool/secrets/passphrase"
)


def set_data_dir(path):
    """Override DATA_DIR for this invocation (called when a directory is
    passed on the command line instead of relying on the env var/default)."""
    global DATA_DIR
    DATA_DIR = path


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_file_config():
    """Read simple key=value lines from CONFIG_FILE, if it exists."""
    cfg = {}
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip()
    return cfg


def get_settings():
    file_cfg = load_file_config()
    settings = {
        "retention_days": int(os.environ.get("RETENTION_DAYS", "7")),
        "backup_prefix": os.environ.get("BACKUP_PREFIX", "backup"),
        "poll_seconds": int(os.environ.get("POLL_SECONDS", "5")),
        # only available via the mounted config file, not env vars -
        # deliberately, so both ConfigMap consumption paths are shown
        "compression_level": int(file_cfg.get("COMPRESSION_LEVEL", "6")),
    }
    return settings


def get_passphrase():
    if os.path.isfile(PASSPHRASE_FILE):
        with open(PASSPHRASE_FILE) as f:
            return f.read().strip()
    return os.environ.get("BACKUP_PASSPHRASE")  # fallback for local testing


def snapshot(data_dir):
    """Return {relative_path: mtime} for every file under data_dir."""
    state = {}
    if not os.path.isdir(data_dir):
        return state
    for root, _dirs, files in os.walk(data_dir):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, data_dir)
            try:
                state[rel] = os.path.getmtime(full)
            except OSError:
                pass
    return state


def make_archive(settings):
    if not os.path.isdir(DATA_DIR) or not os.listdir(DATA_DIR):
        log(f"nothing to back up in {DATA_DIR}, skipping")
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_name = f"{settings['backup_prefix']}-{timestamp}.tar.gz"
    archive_path = os.path.join(BACKUP_DIR, base_name)

    with gzip.GzipFile(
        archive_path, mode="wb", compresslevel=settings["compression_level"]
    ) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            tar.add(DATA_DIR, arcname=".")

    log(f"created archive {base_name} (compression level {settings['compression_level']})")

    passphrase = get_passphrase()
    if passphrase:
        encrypted_path = archive_path + ".gpg"
        subprocess.run(
            [
                "gpg", "--batch", "--yes", "--passphrase", passphrase,
                "--symmetric", "--cipher-algo", "AES256",
                "--output", encrypted_path, archive_path,
            ],
            check=True,
        )
        os.remove(archive_path)
        log(f"encrypted archive -> {os.path.basename(encrypted_path)}")
        archive_path = encrypted_path
    else:
        log("no passphrase found - archive left unencrypted")

    apply_retention(settings)
    return archive_path


def apply_retention(settings):
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings["retention_days"])
    if not os.path.isdir(BACKUP_DIR):
        return
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            os.remove(path)
            log(f"removed archive older than {settings['retention_days']}d: {name}")


def cmd_run(args):
    if args.directory:
        set_data_dir(args.directory)
    settings = get_settings()
    make_archive(settings)


def cmd_watch(args):
    if args.directory:
        set_data_dir(args.directory)
    settings = get_settings()
    log(
        f"watching {DATA_DIR} every {settings['poll_seconds']}s "
        f"(prefix={settings['backup_prefix']}, retention={settings['retention_days']}d)"
    )
    last_state = snapshot(DATA_DIR)
    # back up whatever is already there on startup
    if last_state:
        make_archive(settings)
    while True:
        time.sleep(settings["poll_seconds"])
        settings = get_settings()  # re-read in case config changed live
        current_state = snapshot(DATA_DIR)
        if current_state != last_state:
            log("change detected in data directory")
            make_archive(settings)
            last_state = current_state


def cmd_list(_args):
    if not os.path.isdir(BACKUP_DIR) or not os.listdir(BACKUP_DIR):
        print("(no archives found)")
        return
    for name in sorted(os.listdir(BACKUP_DIR)):
        path = os.path.join(BACKUP_DIR, name)
        size_kb = os.path.getsize(path) / 1024
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{name}\t{size_kb:.1f} KB\t{mtime}")


def cmd_restore(args):
    if args.directory:
        set_data_dir(args.directory)
    name = args.archive
    archive_path = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(archive_path):
        print(f"error: archive not found: {name}", file=sys.stderr)
        sys.exit(1)

    work_path = archive_path
    if archive_path.endswith(".gpg"):
        passphrase = get_passphrase()
        if not passphrase:
            print("error: this archive is encrypted but no passphrase is available", file=sys.stderr)
            sys.exit(1)
        work_path = archive_path[: -len(".gpg")] + ".decrypted"
        subprocess.run(
            [
                "gpg", "--batch", "--yes", "--passphrase", passphrase,
                "--decrypt", "--output", work_path, archive_path,
            ],
            check=True,
        )

    os.makedirs(DATA_DIR, exist_ok=True)
    with gzip.GzipFile(work_path, mode="rb") as gz:
        with tarfile.open(fileobj=gz, mode="r") as tar:
            tar.extractall(DATA_DIR)

    if work_path != archive_path:
        os.remove(work_path)

    log(f"restored {name} into {DATA_DIR}")


def main():
    parser = argparse.ArgumentParser(description="Tiny directory backup tool")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Create one archive of a directory right now")
    run_parser.add_argument(
        "directory", nargs="?", default=None,
        help="Directory to archive (default: $DATA_DIR, else /data)",
    )
    run_parser.set_defaults(func=cmd_run)

    watch_parser = sub.add_parser("watch", help="Poll a directory and archive on change")
    watch_parser.add_argument(
        "directory", nargs="?", default=None,
        help="Directory to watch (default: $DATA_DIR, else /data)",
    )
    watch_parser.set_defaults(func=cmd_watch)

    sub.add_parser("list", help="List archives in BACKUP_DIR").set_defaults(func=cmd_list)

    restore_parser = sub.add_parser("restore", help="Restore an archive into a directory")
    restore_parser.add_argument("archive", help="Archive filename, as shown by `list`")
    restore_parser.add_argument(
        "directory", nargs="?", default=None,
        help="Directory to restore into (default: $DATA_DIR, else /data)",
    )
    restore_parser.set_defaults(func=cmd_restore)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
