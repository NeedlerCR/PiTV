#!/usr/bin/env bash
# /opt/pitv/tv-state.sh
#
# Called by homebridge-cmd4 to get/set Television characteristics.
# Invocation:
#   tv-state.sh Get active
#   tv-state.sh Set active 1        (1 = on, 0 = standby)
#   tv-state.sh Get activeidentifier
#   etc.
#
# cmd4 characteristic names arrive in whatever casing the config uses.
# We lowercase both sides for safe matching.

ACTION="${1,,}"   # get or set
CHAR="${2,,}"     # characteristic name (lowercased)
VALUE="${3:-}"

case "${ACTION}/${CHAR}" in

    "get/active")
        # We can't reliably poll CEC state without holding the bus,
        # so report 1 (on). HomeKit will reflect what you last set.
        echo 1
        ;;

    "set/active")
        if [[ "$VALUE" == "1" ]]; then
            /opt/pitv/cec-cmd.sh 'on 0'
        else
            /opt/pitv/cec-cmd.sh 'standby 0'
        fi
        ;;

    "get/activeidentifier")
        echo 1
        ;;

    "set/activeidentifier")
        # Only one input (HDMI 2 / Pi), nothing to switch.
        ;;

    "get/configuredname")
        echo "TV"
        ;;

    "set/configuredname")
        # Accept but ignore renames from HomeKit.
        ;;

    "get/sleepdiscoverymode")
        # 1 = AlwaysDiscoverable
        echo 1
        ;;

    "get/currentmediastate")
        echo 0
        ;;

    "get/targetmediastate")
        echo 0
        ;;

    *)
        # cmd4 requires a 0-exit even for unhandled characteristics
        echo 0
        ;;

esac

exit 0
