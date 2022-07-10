#!/bin/bash

#
# This script fetches the current MoaT code.
# It then calls "setup2.sh" which adds the necessary hooks to Venus.

d="$(cd "$(dirname $0)"; pwd)"
cd $d

set -ex
trap 'echo ERROR' 0 1 2

echo "Victron/MoaT setup"
/opt/victronenergy/swupdate-scripts/resize2fs.sh

# Packages

opkg update
opkg install \
	python3-pip \
	python3-venv \
	python3-modules \
	findutils \
	psmisc \
	git \
	vim \
	lsof \
	binutils \
	git-perltools \
	perl-module-lib \
	perl-module-file-temp \
	perl-module-ipc-open2 \
	perl-module-time-local \


if test ! -e .git ; then
	d=/data/moat
	if test -d $d ; then
		cd $d
		git pull
	else
		git clone --recurse-submodules https://github.com/M-o-a-T/moat-victron.git $d
		cd $d
	fi
fi

# venv setup
if test -d env; then
	python3 -mvenv --upgrade env
else
	python3 -mvenv env
fi

ln -sf $(pwd)/victron env/lib/python*/site-packages/
ln -sf $(pwd)/deframed/deframed env/lib/python*/site-packages/
cd bus/python
ln -sf $(pwd)/msgpack.py env/lib/python*/site-packages/
ln -sf $(pwd)/serialpacker.py env/lib/python*/site-packages/
ln -sf $(pwd)/moat env/lib/python*/site-packages/
cd ../..

. env/bin/activate

env/bin/pip3 install -r requirements.txt
env/bin/pip3 install --upgrade pip

exec ./setup2.sh $d
