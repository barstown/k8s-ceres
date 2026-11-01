customization:
  systemExtensions:
    officialExtensions:
      - siderolabs/iscsi-tools
      # - siderolabs/nfsd
      - siderolabs/qemu-guest-agent
      # {{- if eq .Node.Role "worker" }}
      # - siderolabs/nvidia-container-toolkit
      # {{- end }}
