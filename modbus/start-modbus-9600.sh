#! /bin/sh

. /opt/victronenergy/serial-starter/run-service.sh

app=$(dirname $0)/dbus-modbus-client.py

export PYTHONPATH=/opt/victronenergy/dbus-modbus-client/:/data/moat/twe_meter

start -x -s $tty -r 9600
