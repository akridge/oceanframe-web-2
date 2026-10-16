#!/usr/bin/env bash
# Kept so older instructions still work: the multi-user stack (Oracle, the app,
# the evaluation worker and the T3 starter data) through deploy/stack_up.sh, which
# writes .env with generated passwords on the first run. Options pass through
# (--fresh, --yes). The single-user compose file is `make up`.
exec bash "$(dirname "$0")/deploy/stack_up.sh" "$@"
