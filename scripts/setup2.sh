#!/bin/bash

if test -z "$1" ; then
	d="$(cd "$(dirname $0)"; pwd)"
else
	d="$1"
fi
cd $d

cp vimrc $HOME/.vimrc
sed -e "s#:DIR:#$d#" < profile.sh > $HOME/.profile

. env/bin/activate

cp -r serial/dbus-modbus-local.svc/. /opt/victronenergy/service-templates/dbus-modbus-local.serial
sed -i -e "s!DIR!$d!" /opt/victronenergy/service-templates/dbus-modbus-local.serial/run

cp -r serial/moat-serial.svc/. /opt/victronenergy/service-templates/moat.serial
sed -i -e "s!DIR!$d!" /opt/victronenergy/service-templates/moat.serial/run

mkdir -p /data/conf/serial-starter.d
cat <<_ >/data/conf/serial-starter.d/lmodbus.conf
service lmodbus         dbus-modbus-local.serial
_

cat <<_ >/data/conf/serial-starter.d/moat.conf
service moat         moat.serial
_

cp serial/udev.rules /etc/udev/rules.d/serial-starter-aux.rules

set +x
echo OK, all done.
trap '' 0
exit 0
