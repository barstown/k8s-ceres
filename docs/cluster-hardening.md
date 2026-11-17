## Cluster Hardening

Plan for hardening the cluster, in priority order. As of 2026-11-16.

### Current State

The cluster is 6 Talos v1.14.6 nodes (3 control plane, 3 workers) on 10.0.50.0/24, running Cilium in native routing mode with Envoy Gateway in front. The biggest remaining gap is the namespaces still without network policies.

| Area | Today | Gap |
| --- | --- | --- |
| Pod-to-pod access | Default deny in 15 app namespaces | The rest are unrestricted, including platform namespaces |
| Traffic between nodes | WireGuard between pods | Host-network traffic plain text (`nodeEncryption` beta) |
| App login | authentik forward auth on 9 apps | OIDC apps still use their own login |
| Edge | CrowdSec bouncer on `envoy-external` | Covers the external gateway only |
| Pod security | Talos PodSecurity admission; `kube-system` privileged | Not reviewed per namespace |
| Visibility | Hubble with UI, metrics and a policy drop alert | None for now |

### 1. Cilium: WireGuard Encryption and Hubble (Easy Win)

This encrypts all pod traffic between nodes, using one Helm values change and no app changes. It keeps traffic private from anything else on the node network. It does not prove which workload is talking (see mutual auth below).

Change in `kubernetes/apps/kube-system/cilium/app/helmrelease.yaml`:

```yaml
encryption:
  enabled: true
  type: wireguard
```

Before rolling out:

- Checked for Cilium 1.20.4: DSR with Geneve dispatch is supported with WireGuard; only non-Geneve DSR leaves redirected north-south traffic unencrypted. Current setting: `loadBalancer.mode: dsr` with `dsrDispatch: geneve`.
- Not now: `encryption.nodeEncryption: true` would also encrypt host-network traffic, but Cilium still marks it beta and skips control-plane nodes by default, so 3 of 6 nodes would stay plain text. Revisit when it leaves beta.
- Expect a short interruption to traffic between nodes as the agents restart; apply at a quiet time.
- Verify with `cilium encrypt status` in an agent pod. Talos includes WireGuard in its kernel.

#### Hubble (Same Change)

Hubble shows every flow Cilium handles: source and destination pod, port, DNS and HTTP details, and whether policy allowed or dropped it. It lets you write network policies from real traffic and see what a new policy broke.

```yaml
hubble:
  enabled: true
  relay:
    enabled: true
  ui:
    enabled: true
  metrics:
    enabled: [dns, drop, tcp, flow, port-distribution, icmp, httpV2]
    serviceMonitor:
      enabled: true
```

- It needs the same agent restart as WireGuard, so one maintenance window covers both.
- After rollout, `hubble observe --verdict DROPPED` shows whether anything broke.
- Hubble UI shows all cluster traffic: route it on `envoy-internal` only and put it behind authentik.
- Cost: a relay pod, a UI pod and some agent memory. Flows are kept in a small in-memory buffer, not stored.

### 2. Network Policies (Biggest Impact)

Move each namespace to default deny, then allow only the flows it needs. This contains a compromised app to its own namespace.

1. Use Hubble (section 1) to see each namespace's real traffic before writing any rules.
2. Write a reusable kustomize component in `kubernetes/components/`: default deny, plus allows for DNS, Envoy gateway ingress and Prometheus scraping.
3. Add per-app allows for its database, outbound internet and the APIs it calls (for example Prowlarr → Sonarr).
4. Roll out one namespace at a time, starting with `authentik` (Postgres reachable only from authentik pods).
5. Last, add cluster-wide rules: block pod access to the node LAN and the Talos/Kubernetes APIs except where needed.

`CiliumNetworkPolicy` can also filter by HTTP path and method where a single port needs finer control.

Done so far: authentik, changedetection, homebox, sonarr, radarr, lidarr, prowlarr, sabnzbd, kapowarr, tautulli, seerr, recyclarr, navidrome, kavita and audiomuse-ai. Pods opt into internet, Kubernetes API and Plex access with `app.ceres.io/egress-*` labels (see `docs/labels.md`).

#### Catching Missed Flows

A Hubble capture only covers its time window. It missed Recyclarr's once-a-day sync, which then failed on 2026-11-16.

- Before writing rules, also check each app's settings (UI-configured URLs, scheduled jobs, plugins) for flows the capture may not show.
- The `CiliumPolicyDenied` alert (`kube-system/cilium-rules`) sends to ntfy within about 10 minutes, naming both ends. It also fires briefly when a namespace first gets policies.
- Hubble's flow buffer holds well under a minute of traffic on busy nodes, so check drops soon after a change.

#### Hostname Rules (toFQDNs) and CoreDNS Autopath

A `toFQDNs` rule on its own does not match in this cluster. Pods use `ndots: 5`, so a name with fewer than 5 dots is first looked up with the search domains. CoreDNS `autopath` answers that expanded query (`<name>.<namespace>.svc.cluster.local`) with a CNAME to the real name, and Cilium records the IP only under the expanded name.

This blocked authentik-postgres backups to TrueNAS for about 11 minutes on 2026-11-15, until a CIDR rule was added. That rule (`10.0.10.50/32`, TCP 30292) now lives in the component's `cnpg-instances` policy, so every CNPG namespace gets backups without its own rule.

- LAN targets with fixed IPs: add a `toCIDR` rule for the IP. Keeping the `toFQDNs` rule alongside it is harmless.
- Hostnames whose IPs change: also match the expanded name, e.g. `matchPattern: "<name>.*.svc.cluster.local"`.
- To check what Cilium recorded, run `cilium-dbg fqdn cache list` in the agent on the pod's node.

App policies may apply a few seconds before the component and briefly deny traffic. That gap is accepted.

### 3. SSO with authentik

Put authentik in front of apps so there is one login, with MFA and group-based access.

- **Apps with OIDC** (Grafana, Immich, Semaphore, Homebox, Kavita): use an authentik OIDC provider.
- **Apps without OIDC** (Sonarr, Radarr, Lidarr, Prowlarr, SABnzbd, Tautulli, Changedetection): use a proxy provider in forward auth mode, plus an Envoy Gateway `SecurityPolicy` with `extAuth` on the app's HTTPRoute. No Istio needed.
- Keep `/api` paths that use API keys outside the proxy, and set each app's own auth to External.
- Fail closed: no `failOpen` on these policies.

Done so far: Changedetection, Hubble UI and the media stack (Sonarr, Radarr, Lidarr, Prowlarr, SABnzbd, Kapowarr, Tautulli).

- Providers, applications and group bindings are managed by the authentik blueprint `kubernetes/apps/authentik/authentik/app/resources/ceres-access.yaml`. Apps are bound to `home-users`, except Hubble, which is `authentik Admins` only. Users and group membership stay in the UI.
- Each HTTPRoute has named `api` and `app` rules, and the `SecurityPolicy` targets the `app` rule.
- Calls between apps go to Services directly and don't pass through authentik.
- Gatus probes these apps on an API path with an annotation, since `/` now redirects to authentik.
- Tautulli has no External auth mode. It needs its admin password cleared and HTTP Proxy enabled (`TAUTULLI_HTTP_PROXY`).

### Deferred: Cilium Mutual Authentication

Deferred while it is beta. It is the closest match to mesh-style mTLS. Cilium uses SPIRE to give each workload a cryptographic identity, and a policy with `authentication.mode: required` makes both ends prove it before traffic flows.

When to revisit:

- It leaves beta in a Cilium release, or its known limitations no longer affect this cluster.
- Network policies (section 2) are in place, since mutual auth is enforced through them.
- The SPIFFE/SPIRE review below is done.

### To Discuss: SPIFFE and SPIRE

This section only lists questions; we'll review them separately. SPIFFE is the standard for workload identity (IDs such as `spiffe://ceres/ns/sonarr/sa/sonarr` and the certificates that prove them). SPIRE is the server and agent that issue them.

- [ ] How SPIRE proves a workload's identity (node and workload attestation) and what it trusts
- [ ] Running SPIRE as Cilium's identity backend versus on its own
- [ ] Where its root CA and data live, and how backup and recovery work
- [ ] What else could use the identities (authentik, Postgres client certs, external services)
- [ ] Running and upgrade cost on Talos

### Checklist

- [x] Enable Cilium WireGuard encryption and Hubble in one change; verify with `cilium encrypt status` and `hubble observe`
- [x] Write the default-deny network policy component
- [x] Lock down the `authentik` namespace
- [x] Alert on network policy drops
- [x] Roll out network policies to the media apps
- [ ] Roll out network policies to the remaining namespaces
- [ ] Decide whether to remove CoreDNS `autopath` before the first internet `toFQDNs` rule
- [x] Put the first app (Changedetection) behind authentik forward auth
- [x] Put Hubble UI and the media stack behind authentik
- [ ] Move apps with OIDC support to authentik OIDC
- [ ] Review PodSecurity levels per namespace
- [ ] Review SPIFFE/SPIRE
- [ ] Revisit Cilium mutual auth once it is out of beta
