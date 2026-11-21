#!/bin/bash
# usage: run_sel.sh MAPNAME PATCHDIR_WIN TESTS_JSON_WIN OUTNAME [selector.py name]   (env: BR_MODE, BR_HEAVY_POLICY, ...)
MAP=$1; PD=$2; TJ=$3; OUTN=$4; SEL=${5:-selector.py}
MSYS_NO_PATHCONV=1 docker run --rm --cpus 2 --memory 4g --pids-limit 256 -v "C:/Users/player/br-work/dev:/dev_tools:ro" -v "C:/Users/player/br-work/out:/out:ro" -v "$PD:/data/commits:ro" -v "$TJ:/data/tests.json:ro" -e BR_MAP_DIR=/out/$MAP -e BR_SRC=/lab/sqlglot -e BR_DEBUG=1 -e BR_MODE=${BR_MODE:-x} -e BR_HEAVY_POLICY=${BR_HEAVY_POLICY:-gate} -e BR_X_BUDGET=${BR_X_BUDGET:-8.0} -e BR_ITSD=${BR_ITSD:-1} -e BR_GATE_RUN=${BR_GATE_RUN:-1} -e HOME=/tmp br-lab sh -c "cat /sys/fs/cgroup/cpu.stat | head -1; python /dev_tools/$SEL; cat /sys/fs/cgroup/cpu.stat | head -1" > /c/Users/player/br-work/out/$OUTN.raw 2> /c/Users/player/br-work/out/$OUTN.err
grep '^{' /c/Users/player/br-work/out/$OUTN.raw > /c/Users/player/br-work/out/$OUTN.jsonl
grep usage_usec /c/Users/player/br-work/out/$OUTN.raw | awk 'NR==1{a=$2} NR==2{printf "container cpu %.2fs\n", ($2-a)/1e6}'
tail -1 /c/Users/player/br-work/out/$OUTN.err
