# bitcast-x-miner Helm chart

Runs one Bitcast X miner on Kubernetes: `bitcast-x run-miner`, or `run-miner-api` for a platform
that adds the authenticated application API. It follows the runtime contract in
[`docs/operator-runbook.md`](../../docs/operator-runbook.md): UID/GID 10001, state on persistent
storage at `/var/lib/bitcast-x`, the wallet under `/var/lib/bitcast-wallets`, miner HTTP on 8095.

One release is one miner. The chart always runs a single replica with the `Recreate` strategy:
a hotkey advertises exactly one endpoint on chain, and the SQLite state has one writer.

## Before you install

1. **Build and push the image.** There is no default registry. Build from this repository's
   `Dockerfile`, push to a registry your cluster pulls from, and pin the tag or digest of a
   reviewed commit:

   ```bash
   docker build --build-arg REVISION="$(git rev-parse HEAD)" -t <registry>/bitcast-x:<version> .
   docker push <registry>/bitcast-x:<version>
   ```

2. **Register the hotkey** on the target subnet, and meet its qualification barrier if it has
   one. The chart does not register anything.

3. **Create the wallet Secret** from the existing hotkey keyfile:

   ```bash
   kubectl create secret generic bitcast-miner-hotkey \
     --from-file=hotkey="$HOME/.bittensor/wallets/<wallet>/hotkeys/<hotkey>"
   ```

### Key handling — read this

The hotkey signs chain extrinsics as your miner: endpoint advertisements and commitments. Anyone
who can read that Secret can act as your miner on chain.

- The chart **never creates the Secret from values**. A key placed in `values.yaml` or `--set`
  would be stored in the Helm release record, which is itself a Secret readable by anyone with
  access to the release. Create it out of band, or with your secret manager's operator.
- The keyfile is mounted **read-only** at the path the miner already reads, readable by UID/GID
  10001 only (`defaultMode: 0440` with `fsGroup: 10001`). It is not passed in an environment
  variable, so it is not visible in the pod spec or the process environment.
- Only the hotkey is needed. Never put the coldkey in the cluster. `coldkeypub.txt` (the public
  half) is optional, via `wallet.coldkeypubKey`.
- Set `wallet.expectedHotkey` to the hotkey's SS58 address. The container then refuses to start
  if the Secret holds a different key.
- Restrict `get`/`list` on Secrets in the miner's namespace with RBAC.

## Exposing the miner

Validators find the miner from its on-chain axon record and dial `http://<publicIP>:<port>`
directly: **a bare IP and port, over plain HTTP**. Requests are authenticated by signature, not
TLS, and there is no hostname — so **an Ingress cannot carry this traffic**. Something must make
the pod reachable at exactly `publicIP` **and** `port`, because the miner advertises the port it
binds.

| `service.type` | `publicIP` is | Notes |
|---|---|---|
| `LoadBalancer` (default) | the load balancer's address | Pin it with `service.loadBalancerIP` or your provider's annotation, so it does not change under a live advertisement. |
| `NodePort` | a node's public address | `port` must be in the NodePort range (30000–32767); it is used as the nodePort. |
| `ClusterIP` | whatever routes to the Service | For clusters where something else maps `publicIP:port` to it. |

`publicIP` is required. The chart refuses to render with it empty, `0.0.0.0` or a loopback
address.

## Install

```bash
helm install miner charts/bitcast-x-miner \
  --set image.repository=<registry>/bitcast-x \
  --set image.tag=<version> \
  --set publicIP=203.0.113.10 \
  --set wallet.existingSecret=bitcast-miner-hotkey \
  --set wallet.expectedHotkey=<hotkey ss58>
```

The pod turns Ready only after the endpoint advertisement is finalized on chain (`GET /ready`). A
restart inside the chain's serving rate limit reuses the advertisement only when it already points
at this pod's endpoint; otherwise the container exits and is restarted.
Liveness is `GET /health`: an idle miner, or one whose chain RPC is briefly unreachable, is not
restarted. Validators retry unavailable miners, which heal on a later poll.

### `run-miner-api`

```bash
kubectl create secret generic bitcast-miner-api \
  --from-literal=token="$(openssl rand -hex 32)"

helm install miner charts/bitcast-x-miner ... \
  --set mode=run-miner-api \
  --set minerApi.existingSecret=bitcast-miner-api
```

By default `/api/v1` is served on the **same port** as the public miner protocol. Call it
in-cluster through the Service, or through your own TLS-terminating proxy. Never send the bearer
token to the public address, which is plain HTTP.

With a bitcast-x build that supports `BITCAST_X_MINER_API_PORT` (added by #138), set `minerApi.port` to give the
API its own listener instead. The public port then carries only the validator protocol. The API
port gets a separate ClusterIP Service (`<release>-api`) and is never added to the miner Service,
whatever its type. To reach it from outside the cluster, enable the TLS Ingress, which routes only
`/api/v1`:

```bash
  --set minerApi.port=8096 \
  --set minerApi.ingress.enabled=true \
  --set minerApi.ingress.host=miner-api.example.com \
  --set minerApi.ingress.annotations."cert-manager\.io/cluster-issuer"=letsencrypt-prod
```

## State, backup and upgrades

State lives on a PersistentVolumeClaim (`persistence.*`). The claim carries
`helm.sh/resource-policy: keep`, so `helm uninstall` does not delete it. Losing it loses
commitment history; see the runbook's recovery section.

Take a transactionally consistent backup before any restart or upgrade. Do not copy live SQLite
files:

```bash
kubectl exec deploy/miner-bitcast-x-miner -- bitcast-x state-info
kubectl exec deploy/miner-bitcast-x-miner -- \
  bitcast-x backup-state --output /var/lib/bitcast-x/backups/<timestamp>
```

Then copy the backup off the volume (`kubectl cp`). Upgrade by changing `image.tag` (or
`image.digest`) to a reviewed build.

## Values

| Key | Default | |
|---|---|---|
| `mode` | `run-miner` | or `run-miner-api` |
| `image.repository` / `tag` / `digest` | `bitcast-x` / appVersion / — | digest wins over tag |
| `network.name` | `finney` | or a websocket URL |
| `network.netuid` / `mechanismId` | `93` / `1` | |
| `publicIP` | — | **required** |
| `port` | `8095` | container, Service and advertised port |
| `service.type` | `LoadBalancer` | see *Exposing the miner* |
| `wallet.existingSecret` | — | **required** |
| `wallet.hotkeyKey` / `coldkeypubKey` | `hotkey` / — | keys in that Secret |
| `wallet.name` / `hotkey` | `default` / `default` | names the miner resolves |
| `wallet.expectedHotkey` | — | strongly recommended |
| `minerApi.existingSecret` / `tokenKey` | — / `token` | `run-miner-api` only |
| `minerApi.port` | — | `run-miner-api` only; separate API listener |
| `minerApi.ingress.enabled` / `host` / `className` / `annotations` / `tlsSecretName` | `false` / — / `traefik` / `{}` / `<release>-api-tls` | needs `minerApi.port` |
| `persistence.enabled` / `size` / `storageClass` / `existingClaim` | `true` / `5Gi` / cluster default / — | |
| `env` | `{}` | extra `BITCAST_X_*` settings |
| `extraEnv` | `[]` | full env entries, e.g. from other Secrets |
| `extraVolumes` / `extraVolumeMounts` | `[]` | e.g. a ConfigMap mounted into the miner |
| `probes.*` | see `values.yaml` | |
