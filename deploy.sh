#!/bin/sh

case "$# $1" in
  "1 plan"|"1 apply") exec uv run railway config "$1" ;;
  *) echo "Usage: $0 {plan|apply}" >&2; exit 2 ;;
esac
