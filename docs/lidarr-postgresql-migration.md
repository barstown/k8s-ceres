# Migrating Lidarr from SQLite to PostgreSQL

How to move Lidarr's database from SQLite on the `lidarr` PVC to its own CNPG
cluster, following the pattern the other PostgreSQL apps use (`homebox`,
`immich`, and so on).

**Status:** plan only. Nothing is staged. Decide first using
[Is it worth it?](#is-it-worth-it), then add the manifests below.

SQLite allows one writer at a time. Lidarr's refreshes, imports, housekeeping
(`VACUUM`) and UI all compete for that lock, and on a ~1 GB `lidarr.db` a
`VACUUM` holds it for minutes. Example from 2026-11-19: the second of two
back-to-back housekeeping runs vacuumed the main database from 17:28:35 to
17:31:55, and an artist refresh logged `database is locked` the whole time.
The `freenas-api-iscsi-sqlite` class shortens those windows. PostgreSQL removes
them.

## Is it worth it?

Watch VictoriaLogs for a few days. Lock errors per day:

```logsql
kubernetes.pod_namespace:=lidarr "database is locked" | stats by (_time:1d) count() locks
```

What was running when they happened (housekeeping, refresh, import):

```logsql
kubernetes.pod_namespace:=lidarr _msg:~"(Running housecleaning|Vacuuming|database compressed|RefreshArtistService: Updating|database is locked$)"
  | sort by (_time)
```

Migrate if locks recur outside housekeeping, or line up with the times the UI
is slow. If they only appear during the daily `VACUUM`, the gain is smaller,
but it still ends the UI stalls during that window.

## Trade-offs

| | |
| --- | --- |
| Gains | No single-writer lock. No `VACUUM` stalls. Point-in-time recovery from WAL archiving |
| Costs | Another 2-instance CNPG cluster. Lidarr's built-in backups stop covering the database (CNPG backups replace them). One short outage to migrate |
| Doesn't fix | UI slowness that comes from Lidarr itself (large library, metadata refreshes) rather than locking |

## Design

| Item | Value | Why |
| --- | --- | --- |
| Cluster | `lidarr-postgres`, 2 instances, namespace `lidarr` | Same as the other CNPG apps |
| PostgreSQL | **18** (`18-standard-trixie`) | Same image as the other clusters. Lidarr's Npgsql 9.x supports 14–18. The wiki only vouches for the SQLite migration on 14–17, so step 6 checks the import closely |
| Storage | 20Gi, `freenas-api-iscsi-postgresql` | 8K volblocksize tuned for PostgreSQL, as `immich` and `audiomuse-ai` use. `lidarr.db` is ~977 MB today |
| Databases | `lidarr-main` (initdb), `lidarr-log` (CNPG `Database` resource) | Lidarr needs both and won't create them. `initdb` only creates one |
| Credentials | `lidarr-db-secret` (SOPS) | Same keys as `homebox-db-secret` |
| Backups | barman-cloud to `s3://k8s-ceres/lidarr`, weekly base backup | Same as `homebox` |
| Network | Add egress to `cnpg.io/cluster: lidarr-postgres` on 5432 in the Lidarr CiliumNetworkPolicy | Lidarr's policy has egress rules, so anything not listed is denied |
| Label | `app.ceres.io/depends-on-postgresql: "true"` | So the `pg-apps` tasks in [postgresql-cnpg-maintenance.md](postgresql-cnpg-maintenance.md) include Lidarr |

The log database starts empty. Lidarr's logs are short-lived and already
collected by VictoriaLogs, so there's no reason to migrate `logs.db`.

## Manifests

All under `kubernetes/apps/lidarr/lidarr/app/`.

### `cluster.yaml`

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/postgresql.cnpg.io/cluster_v1.json
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: lidarr-postgres
  annotations:
    cnpg.io/hibernation: "off"
spec:
  # https://github.com/cloudnative-pg/postgres-containers/pkgs/container/postgresql/versions?filters%5Bversion_type%5D=tagged
  instances: 2
  affinity:
    enablePodAntiAffinity: true
    podAntiAffinityType: preferred
    topologyKey: kubernetes.io/hostname
  imageName: ghcr.io/cloudnative-pg/postgresql:18-standard-trixie@sha256:37fdeea29a4d3ed14570f719488ba55bd21e8975eaea864be5ce74d25a9a13de
  bootstrap:
    initdb:
      database: lidarr-main
      owner: lidarr
      secret:
        name: lidarr-db-secret
  plugins:
    - name: barman-cloud.cloudnative-pg.io
      isWALArchiver: true
      parameters:
        barmanObjectName: rustfs-s3
  primaryUpdateMethod: switchover
  smartShutdownTimeout: 15
  resources:
    requests:
      cpu: 50m
      memory: 256Mi
  storage:
    size: 20Gi
    storageClass: freenas-api-iscsi-postgresql
```

### `database.yaml`

```yaml
---
# yaml-language-server: $schema=https://k8s-schemas.home-operations.com/postgresql.cnpg.io/database_v1.json
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata:
  name: lidarr-log
spec:
  name: lidarr-log
  owner: lidarr
  cluster:
    name: lidarr-postgres
```

### `objectstore.yaml` and `scheduledbackup.yaml`

Copy `homebox`'s, changing `destinationPath` to `s3://k8s-ceres/lidarr` and the
backup's name and cluster to `lidarr-postgres-backup` and `lidarr-postgres`.

### `secret.sops.yaml`

Add a second document. Use an alphanumeric password: the pgloader step puts it
in a connection URL, where other characters would need URL encoding.

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: lidarr-db-secret
stringData:
  database: lidarr-main
  host: lidarr-postgres-rw.lidarr.svc.cluster.local
  password: <generated, alphanumeric>
  port: "5432"
  username: lidarr
```

### `kustomization.yaml`

Add `./cluster.yaml`, `./database.yaml`, `./objectstore.yaml` and
`./scheduledbackup.yaml`.

### `ciliumnetworkpolicy.yaml`

Add to `egress` (no namespace label, so it matches pods in `lidarr`):

```yaml
    - toEndpoints:
        - matchLabels:
            cnpg.io/cluster: lidarr-postgres
      toPorts:
        - ports:
            - port: "5432"
              protocol: TCP
```

Update the policy's `description` to mention PostgreSQL.

### `helmrelease.yaml`

Add the label, as `homebox` does:

```yaml
spec:
  postRenderers:
    - kustomize:
        patches:
          - target:
              group: apps
              kind: Deployment
              version: v1
            patch: |
              - op: add
                path: "/metadata/labels/app.ceres.io~1depends-on-postgresql"
                value: "true"
```

Add to the `app` container's `env`:

```yaml
              LIDARR__POSTGRES__HOST:
                valueFrom:
                  secretKeyRef:
                    name: &dbSecret lidarr-db-secret
                    key: host
              LIDARR__POSTGRES__PORT:
                valueFrom:
                  secretKeyRef:
                    name: *dbSecret
                    key: port
              LIDARR__POSTGRES__USER:
                valueFrom:
                  secretKeyRef:
                    name: *dbSecret
                    key: username
              LIDARR__POSTGRES__PASSWORD:
                valueFrom:
                  secretKeyRef:
                    name: *dbSecret
                    key: password
              LIDARR__POSTGRES__MAINDB: lidarr-main
              LIDARR__POSTGRES__LOGDB: lidarr-log
```

Keep the `config` PVC. It still holds `config.xml`, `MediaCover`, and the
SQLite files you'd roll back to.

## Migration

Expect about 30 minutes of Lidarr downtime, mostly the copy in step 6.

### 1. Back up

In Lidarr: **System → Backup → Backup Now**. The SQLite file on the PVC is the
real rollback point (pgloader only reads a copy of it), but a fresh backup zip
costs nothing.

### 2. Deploy the database (PR 1)

Merge everything above **except** the `LIDARR__POSTGRES__*` env vars. Lidarr
keeps running on SQLite.

```bash
kubectl -n lidarr get cluster lidarr-postgres
kubectl -n lidarr get database lidarr-log -o jsonpath='{.status.applied}{"\n"}'
```

Expected: `Cluster in healthy state`, and `true`.

### 3. Create the schema (PR 2)

Merge the env vars. Lidarr restarts against the empty PostgreSQL databases and
creates the schema. It will briefly show an empty library. That's expected.

```bash
kubectl -n lidarr logs deploy/lidarr -c app | grep -i -E "postgres|migrat|error" | head
```

Once the UI loads, stop Lidarr (step 4) before it starts scheduled tasks on the
empty database.

### 4. Stop Lidarr

Suspend Flux first so it doesn't scale Lidarr back up.

```bash
flux -n lidarr suspend helmrelease lidarr
kubectl -n lidarr scale deploy lidarr --replicas=0
kubectl -n lidarr wait --for=delete pod -l app.kubernetes.io/name=lidarr --timeout=120s
```

### 5. Clear the seed data

Lidarr seeds default profiles when it creates the schema. pgloader would
collide with them.

```bash
kubectl -n lidarr exec -it lidarr-postgres-1 -c postgres -- psql -d lidarr-main
```

If `lidarr-postgres-1` isn't the primary, use the pod from
`kubectl -n lidarr get cluster lidarr-postgres -o jsonpath='{.status.currentPrimary}'`.

```sql
DELETE FROM "QualityProfiles";
DELETE FROM "QualityDefinitions";
DELETE FROM "DelayProfiles";
DELETE FROM "Metadata";
DELETE FROM "MetadataProfiles";
```

### 6. Copy the data with pgloader

Apply this Job by hand; don't commit it. It copies `lidarr.db` and its WAL
files to scratch space so the original stays untouched, then loads them with
the [Roxedus pgloader build](https://github.com/Roxedus/Pgloader-bin) the
Servarr wiki recommends. The pgloader in the Debian and Ubuntu repos is too old
for PostgreSQL 14+. The image only publishes `latest`, so it's pinned by digest.

```yaml
---
apiVersion: batch/v1
kind: Job
metadata:
  name: lidarr-pgloader
  namespace: lidarr
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 2008
        runAsGroup: 2008
        fsGroup: 2001
        seccompProfile: { type: RuntimeDefault }
      initContainers:
        - name: copy
          image: docker.io/library/busybox:1.37.0
          command: ["sh", "-c", "cp -v /config/lidarr.db* /work/"]
          securityContext: &containerSecurity
            allowPrivilegeEscalation: false
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: config, mountPath: /config, readOnly: true }
            - { name: work, mountPath: /work }
      containers:
        - name: pgloader
          image: ghcr.io/roxedus/pgloader@sha256:1a7a86ad56623c00ee714ee4969913ed5c6f59ac9785073e2ffd1bea9cc54d31
          env:
            - name: DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: lidarr-db-secret
                  key: password
          args:
            - --with
            - quote identifiers
            - --with
            - data only
            - --with
            - prefetch rows = 100
            - --with
            - batch size = 1MB
            - /work/lidarr.db
            - postgresql://lidarr:$(DB_PASSWORD)@lidarr-postgres-rw.lidarr.svc.cluster.local/lidarr-main
          securityContext: *containerSecurity
          volumeMounts:
            - { name: work, mountPath: /work }
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: lidarr
        - name: work
          emptyDir:
            sizeLimit: 3Gi
```

The `prefetch rows` and `batch size` options are the wiki's fix for large
databases; at ~1 GB, use them from the start.

```bash
kubectl apply -f lidarr-pgloader.yaml
kubectl -n lidarr logs -f job/lidarr-pgloader
```

pgloader ends with a per-table summary. Every table's `errors` column must be
`0`, and `read` must equal `imported`. If not, stop here and roll back.

The Servarr wiki doesn't guarantee this migration on PostgreSQL 18 (its
community script is tested on 14–17). The summary check above and the write
test in [Verify](#verify) are what catch a bad import. The fallback, if 18
gives trouble, is to roll back and either migrate to a 17 cluster or start
Lidarr on a fresh 18 database and re-add the library.

### 7. Reset sequences

The wiki lists this as a fix for sequence errors. Run it every time: a sequence
left behind the copied IDs only fails later, on the next insert. On an empty
table `MAX()` is `NULL` and `setval` does nothing.

Run the full `select setval(...)` list from the
[Servarr wiki](https://wiki.servarr.com/lidarr/postgres-setup#migrating-data)
in the same `psql` session as step 5. It's about 40 lines, one per table; copy
it from the wiki rather than from here, so it matches the current schema.

### 8. Start Lidarr

```bash
flux -n lidarr resume helmrelease lidarr
kubectl -n lidarr rollout status deploy/lidarr
```

## Verify

- The UI shows your artists, albums, root folders, indexers and download
  clients. **Activity → History** has your past history.
- No errors at startup:

  ```bash
  kubectl -n lidarr logs deploy/lidarr -c app | grep -i -E "error|exception|duplicate key|sequence" | head
  ```

- **System → Status** lists the database as PostgreSQL.
- Add something small (a tag, or a manual search) to confirm writes work. A
  missed sequence shows up here as a `duplicate key` error.
- The first CNPG backup completed:

  ```bash
  kubectl -n lidarr get backups.postgresql.cnpg.io
  ```

- Over the next days, the lock query from [Is it worth it?](#is-it-worth-it)
  returns nothing.

## Rollback

Revert PR 2 (the env vars). Lidarr restarts on the untouched `lidarr.db`.
Anything changed while on PostgreSQL (new artists, grabs, history) is lost, so
decide early. The database resources from PR 1 can stay or be reverted
separately.

## Afterwards

After a week or two without problems:

- **SQLite files.** Delete `lidarr.db`, `lidarr.db-shm`, `lidarr.db-wal`,
  `logs.db*` from the `lidarr` PVC. That ends the rollback option.
- **Config PVC class.** With no SQLite on it, the PVC no longer needs
  `freenas-api-iscsi-sqlite`. Moving it is optional and needs a copy to a new
  PVC.
- **Backups.** Lidarr's own backup zips now only contain `config.xml`. The
  CNPG backups are what protect the library.
