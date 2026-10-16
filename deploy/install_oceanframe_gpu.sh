#!/usr/bin/env bash
# OceanFrame on an NVIDIA GPU workstation: the same installer, GPU variant.
exec env OCEANFRAME_INSTALL_VARIANT=gpu bash "$(dirname "${BASH_SOURCE[0]}")/install_oceanframe.sh" "$@"
