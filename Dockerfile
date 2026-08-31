FROM python:3.12-alpine

# gnupg gives us `gpg` for optional passphrase-based archive encryption.
# tar is required by `kubectl cp` (it exec's tar inside the container to
# stream files in/out) - Alpine's busybox usually provides it, but this
# makes it explicit rather than relying on that.
RUN apk add --no-cache gnupg tar

COPY backup_tool.py /usr/local/bin/backup_tool.py
RUN chmod +x /usr/local/bin/backup_tool.py

# Convenience symlink so the CLI can be invoked as `backup-tool`
RUN ln -s /usr/local/bin/backup_tool.py /usr/local/bin/backup-tool

# Baked into the image so the tool works with no mounts at all (this is
# what makes Act 1's data-loss scenario possible in the first place -
# /data exists in the container's writable layer, but nothing backs it,
# so it vanishes the moment the container is removed).
RUN mkdir -p /data /backups

# Default locations - overridable via env vars of the same name
ENV DATA_DIR=/data \
    BACKUP_DIR=/backups \
    CONFIG_FILE=/etc/backup-tool/backup.conf \
    PASSPHRASE_FILE=/etc/backup-tool/secrets/passphrase \
    RETENTION_DAYS=7 \
    BACKUP_PREFIX=backup \
    POLL_SECONDS=5

# Default to watching DATA_DIR; override with a directory argument to
# watch/archive somewhere else without changing the env var, e.g.:
#   docker run ... backup-tool:latest backup-tool watch /uploads
CMD ["backup-tool", "watch"]
