#!/bin/bash
set -e

if ! test -d /opt/debian/bin ; then
	echo "This script finalizes your Debian-plus-Victron installation." >&2
	echo "You need to run it on Venus." >&2
	exit 1
fi

mkdir -p /usr/local/bin
mkdir -p /usr/local/sbin

test -x /usr/local/bin/btrfs && exit 0

chroot /opt/debian apt-get update
chroot /opt/debian apt-get install -y btrfs-progs
cat >/usr/local/bin/btrfs <<'_'

#!/bin/sh

exec env PATH=/opt/debian/bin LD_LIBRARY_PATH=/opt/debian/lib/aarch64-linux-gnu/ \
    /opt/debian/bin/$(basename $0) \
    "$@"
_

chmod +x /usr/local/bin/btrfs
