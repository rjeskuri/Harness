#!/bin/sh
# The live smoke check: scripts/deploy_guard.py runs this after each deploy of main, against the live system.
# Contract: exit 0 healthy; 1 big failure, which reverts the merge, redeploys and opens an incident;
# 2 degraded, which opens or comments on a `degraded` issue and reverts nothing.
# Replace this stub with the live checks the project declares.
exit 0
