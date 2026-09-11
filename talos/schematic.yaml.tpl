customization:
  systemExtensions:
    officialExtensions:
      - siderolabs/intel-ucode
      - siderolabs/iscsi-tools
      - siderolabs/nfsd
      - siderolabs/qemu-guest-agent
      # {{- if eq .Node.Role "worker" }}
      # - siderolabs/nvidia-container-toolkit
      # {{- end }}
