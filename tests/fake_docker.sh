#!/bin/sh
# Installer unit-test fixture; never invokes the real daemon.
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  version) printf '%s\n' "${FAKE_DOCKER_VERSION:-20.10.16}";;
  container) [ "${FAKE_DOCKER_EXISTS:-0}" = 1 ];;
  build) exit 1;;
  *) exit 99;;
esac
