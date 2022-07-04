#!/bin/bash

#
# Prepare a Venus image for MoaT and whatnot

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

cp vimrc $HOME/.vimrc
cp bashrc $HOME/.bashrc

# venv
if test -d env; then
	python3 -mvenv --upgrade env
else
	python3 -mvenv env
fi


set +x
. $d/env/bin/activate
set -x

$d/env/bin/pip3 install -r requirements.txt
$d/env/bin/pip3 install --upgrade pip

cp -r serial/dbus-modbus-local.svc/. /opt/victronenergy/service-templates/dbus-modbus-local.serial
sed -i -e "s!DIR!$d!" /opt/victronenergy/service-templates/dbus-modbus-local.serial/run

mkdir -p /data/conf/serial-starter.d
echo <<_ >>/data/conf/serial-starter.d/lmodbus.conf
service lmodbus         dbus-modbus-local.serial
_

cp serial/udev.rules /etc/udev/rules.d/serial-starter-aux.rules

ln -sf $(pwd)/victron $d/env/lib/python*/site-packages/
cd bus/python
ln -sf $(pwd)/msgpack.py $d/env/lib/python*/site-packages/
ln -sf $(pwd)/serialpacker.py $d/env/lib/python*/site-packages/
ln -sf $(pwd)/moat $d/env/lib/python*/site-packages/
cd ../..

set +x
echo OK, all done.
trap '' 0
exit 0
