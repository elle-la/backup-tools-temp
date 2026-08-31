# backup-tool

A tiny CLI that archives a data directory - on demand or on a watch loop -
built to walk through Docker volumes/bind mounts, Kubernetes
ConfigMaps/Secrets, and Docker data-loss troubleshooting in one continuous
story.

## Lab topology

This repo is the one the lab's CloudFormation template clones into
`/home/cloud_user/backup-tool` **on the workstation only**. The workstation
(10.0.1.104) has Docker, `kubectl`, and a working kubeconfig already set up
by the time you log in - run everything below from there.

The workstation is *not* a cluster node. The Kubernetes cluster is the
controller (10.0.1.101, control-plane, tainted so it won't run workloads)
plus node1 (10.0.1.102) and node2 (10.0.1.103) - and there's no shared
filesystem between any of them. Act 3 works around that by using
`kubectl cp` to copy a file from the workstation straight into the running
container, rather than relying on a directory that would need to live on
whichever node the Pod happens to land on.

A local, unauthenticated Docker registry is already running on the
workstation at `10.0.1.104:5000`, and node1/node2's containerd is already
configured to trust it - that's what you'll push the image to and what the
Pod will pull it from.

## CLI

```
backup-tool run [DIR]                 One-shot: archive DIR right now
backup-tool watch [DIR]               Poll DIR every POLL_SECONDS and
                                       archive whenever something changes
backup-tool list                      List archives currently in BACKUP_DIR
backup-tool restore <archive> [DIR]   Extract an archive back into DIR
```

`DIR` is optional - omit it and the tool falls back to the `DATA_DIR` env
var, then to `/data`. Passing it directly means the same image/container
can be pointed at any directory without an env var change or rebuild.

Config: `RETENTION_DAYS`, `BACKUP_PREFIX`, `POLL_SECONDS` (env vars) and
`COMPRESSION_LEVEL` (only via the config file at `/etc/backup-tool/backup.conf`,
by design - so both ConfigMap consumption patterns are shown).

Secret: if `/etc/backup-tool/secrets/passphrase` exists, archives are
encrypted with `gpg` and `restore` requires the same passphrase to decrypt.

## Project layout

`data/` is the host-side "drop files here" directory used in Acts 1 and 2,
which run entirely on the workstation (it has Docker installed, so this
works exactly like a normal local Docker setup). It's empty (just a
`.gitkeep`) until you start writing to it below.

Act 3 does **not** use this `data/` directory directly - it copies a file
out of it into the running Pod with `kubectl cp` instead (see below).

## Build and push

```bash
cd ~/backup-tool          # where the CloudFormation UserData cloned this repo
docker build -t backup-tool:latest .

# Push to the workstation's own local registry so the cluster nodes can pull it
docker tag backup-tool:latest 10.0.1.104:5000/backup-tool:latest
docker push 10.0.1.104:5000/backup-tool:latest
```

## Act 1 - reproduce the incident (no volume)

```bash
docker run -d --name backup-tool backup-tool:latest
docker exec backup-tool backup-tool run      # nothing in /data yet, that's fine
docker exec backup-tool sh -c 'echo hi > /data/note.txt'
docker exec backup-tool backup-tool run
docker exec backup-tool backup-tool list     # archive exists

docker rm -f backup-tool
docker run -d --name backup-tool backup-tool:latest
docker exec backup-tool backup-tool list     # (no archives found) - gone!

docker inspect backup-tool --format '{{json .Mounts}}'   # empty []
```

## Act 2 - fix it with a named volume + bind mount

The directory to archive is passed on the command line here (`/uploads`),
so the bind mount can point anywhere on the host without touching the
image or an env var. Run these from inside the `backup-tool` project
directory - `data/` there is the host-side location being bind-mounted.

```bash
docker volume create backup-archives

docker rm -f backup-tool
docker run -d --name backup-tool \
  -v backup-archives:/backups \
  -v "$(pwd)/data:/uploads" \
  backup-tool:latest backup-tool watch /uploads

echo "hello" > ./data/note.txt      # visible in the container immediately
docker exec backup-tool backup-tool run /uploads
docker exec backup-tool backup-tool list

docker rm -f backup-tool
docker run -d --name backup-tool \
  -v backup-archives:/backups \
  -v "$(pwd)/data:/uploads" \
  backup-tool:latest backup-tool watch /uploads
docker exec backup-tool backup-tool list   # archive survived the restart

docker volume inspect backup-archives
```

## Act 3 - move to Kubernetes

The Pod spec (`k8s/pod.yaml`) isn't pinned to any particular node - its
`/uploads` volume is an `emptyDir`, populated via `kubectl cp` rather than
a live host mount, so it doesn't matter which node the Pod lands on. Its
image already points at `10.0.1.104:5000/backup-tool:latest` (the registry
from "Build and push" above) - nothing to substitute at apply time.

```bash
kubectl create configmap backup-env-config \
  --from-literal=RETENTION_DAYS=7 \
  --from-literal=BACKUP_PREFIX=prod \
  --from-literal=POLL_SECONDS=5

kubectl create configmap backup-file-config --from-file=backup.conf

kubectl create secret generic backup-passphrase \
  --from-literal=passphrase='correct-horse-battery-staple'

kubectl apply -f k8s/pod.yaml

# env-var ConfigMap
kubectl exec backup-tool -- printenv RETENTION_DAYS BACKUP_PREFIX POLL_SECONDS

# file-mounted ConfigMap
kubectl exec backup-tool -- cat /etc/backup-tool/backup.conf

# Secret in action - copy a file from the workstation straight into the
# running container's /uploads directory (this is the one step that
# behaves differently from Docker's live bind mount: it's a one-time copy,
# not something the running Pod watches for automatically)
echo "hello from the workstation" > ./data/note.txt
kubectl cp ./data/note.txt backup-tool:/uploads/note.txt

kubectl exec backup-tool -- backup-tool list      # newest entry ends in .gpg

kubectl logs backup-tool     # watch mode noticing the file and encrypting it
```

Point the Pod at a different directory without touching the image by
changing the container's `args` and the matching `mountPath` together,
e.g. `args: ["watch", "/incoming"]` with `mountPath: /incoming` - and
`kubectl cp` into that new path instead of `/uploads`.
