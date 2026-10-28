# Rotating the SOPS age Key to Post-Quantum (ML-KEM-768 + X25519)

Expand-then-contract rotation: every file is encrypted to both the old and new key until all decryptors are verified, so Flux never loses the ability to decrypt.

## Where the key is used

- `.sops.yaml`: recipient for the `talos/` and `bootstrap|kubernetes/` creation rules.
- `flux-system/sops-age` Secret (from `bootstrap/sops-age.sops.yaml`): read by kustomize-controller via `--sops-age-secret=sops-age`.
- `~/.config/sops/age/keys.txt`: local identity (`SOPS_AGE_KEY_FILE` in `.mise/config.toml`).
- `talos/secrets.sops.yaml`: decrypted by topf, which shells out to the local `sops` binary.

## Prerequisites

- Local `age` >= 1.3 and a `sops` release that supports PQ keys (both pinned in `.mise/config.toml`). This also covers topf.
- kustomize-controller with PQ age support (added February 2026).

## 1. Generate and back up the new key

```sh
age-keygen -pq -o ~/.config/sops/age/keys-pq.txt
cat ~/.config/sops/age/keys-pq.txt >> ~/.config/sops/age/keys.txt
age-keygen -y ~/.config/sops/age/keys-pq.txt   # PQ public key (age1pq1...)
```

Store an offline backup of the new private key before continuing.

## 2. Add the new identity to the cluster

Edit the Secret and add the PQ private key on a new line in `age.agekey` (keep the old key):

```sh
sops bootstrap/sops-age.sops.yaml
sops exec-file bootstrap/sops-age.sops.yaml "kubectl apply --filename {}"
kubectl --namespace flux-system rollout restart deployment kustomize-controller
```

## 3. Encrypt all files to both keys

Replace each `age:` string in `.sops.yaml` with a key group containing both recipients:

```yaml
key_groups:
  - age:
      - age1vcetgycw8e62xe34yfu3kqxcxrcrht0ddacvtae9ardxg6qqqyqqskllu6
      - age1pq1...
```

Then re-wrap the data key in every file (metadata-only change):

```sh
find . -name '*.sops.yaml' -not -path './.git/*' -exec sops updatekeys --yes {} \;
```

## 4. Commit and verify

- Push, then check `flux get kustomizations --all-namespaces` for decryption errors.
- Prove the PQ path works: temporarily apply the Secret with only the PQ key, reconcile, and confirm success.
- Run a topf command that reads `talos/secrets.sops.yaml`.

## 5. Remove the old key

1. Remove the old recipient from `.sops.yaml` and re-run the `updatekeys` command from step 3.
2. Optionally rotate each file's data key: `sops rotate --in-place <file>`.
3. Remove the old key from `bootstrap/sops-age.sops.yaml`, re-apply it, and restart kustomize-controller.
4. Remove the old identity from `~/.config/sops/age/keys.txt`, keeping an offline archive copy to decrypt git history.

## Caveat

Git history stays encrypted to the old X25519 key. If harvest-now-decrypt-later is the concern, rotate the underlying sensitive credentials after the cutover.
