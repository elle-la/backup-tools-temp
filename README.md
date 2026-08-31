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
plus node1 (10.0.1.102) and node2 (10.0.1.103). That separation matters in
Act 3: anything a Pod's `hostPath` volume reads has to exist on whichever
node the Pod is actually running on, not on the workstation where this repo
lives. More on that below.

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

Act 3 does **not** use this `data/` directory - see the topology note above
and the walkthrough below for why, and where the equivalent directory
actually needs to live instead.

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

The Pod spec (`k8s/pod.yaml`) is pinned to `node1` on purpose, and its image
already points at `10.0.1.104:5000/backup-tool:latest` (the registry from
"Build and push" above) - nothing to substitute at apply time.

```bash
kubectl create configmap backup-env-config \
  --from-literal=RETENTION_DAYS=7 \
  --from-literal=BACKUP_PREFIX=prod \
  --from-literal=POLL_SECONDS=5

kubectl create configmap backup-file-config --from-file=backup.conf

kubectl create secret generic backup-passphrase \
  --from-literal=passphrase='correct-horse-battery-staple'
```

Before applying the Pod, create the directory it expects - **on node1**,
not the workstation, since that's where the Pod is pinned and `hostPath`
only ever resolves locally on whichever node the Pod runs on:

```bash
# Run from the workstation - it already has sshpass and the node's password
sshpass -p '<password>' ssh -o StrictHostKeyChecking=no cloud_user@10.0.1.102 \
  "mkdir -p /home/cloud_user/backup-tool/data"
```

```bash
kubectl apply -f k8s/pod.yaml

# env-var ConfigMap
kubectl exec backup-tool -- printenv RETENTION_DAYS BACKUP_PREFIX POLL_SECONDS

# file-mounted ConfigMap
kubectl exec backup-tool -- cat /etc/backup-tool/backup.conf

# Secret in action - drop a file into the directory on node1 (again, not the
# workstation's local ./data - that's a different machine and the Pod never
# sees it)
sshpass -p '<password>' ssh -o StrictHostKeyChecking=no cloud_user@10.0.1.102 \
  "echo 'hello from node1' > /home/cloud_user/backup-tool/data/note.txt"

kubectl exec backup-tool -- backup-tool list      # newest entry ends in .gpg

kubectl logs backup-tool     # watch mode noticing the file and encrypting it
```

Point the Pod at a different directory without touching the image by
changing three things together: `nodeName`, the volume's `hostPath`, and
the container's `args`/matching `mountPath` - and remember to create the
new directory on whichever node you pick before the Pod starts.
