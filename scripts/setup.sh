#!/bin/bash

#
# This script fetches the current MoaT code.
# It then calls "setup2.sh" which adds the necessary hooks to Venus.

if test -n "$1" ; then d=$1 ; mkdir -p $d; else
d=/data/moat
fi
cd $d

set -ex
trap 'echo ERROR' 0 1 2

echo "Victron/MoaT setup"
/opt/victronenergy/swupdate-scripts/resize2fs.sh

# Packages

opkg update
opkg install \
	gcc \
	g++ \
	python3-pip \
	python3-venv \
	python3-modules \
	python3-dev \
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


if test -e .git && test -s ./scripts/setup.sh ; then
	d=$(pwd)
else
	d=/data/moat
	if test -d $d ; then
		cd $d
		git pull
	else
		git clone https://github.com/M-o-a-T/moat-victron.git $d
		cd $d
		git submodule update --init bus
		git submodule update --init deframed
		cd bus
		git submodule update --init python/lib/serialpacker
		git submodule update --init python/moat/util
		cd ..
	fi
	git checkout --recurse-submodules
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

./scripts/webstuff-dl $d
exec ./scripts/setup2.sh $d
