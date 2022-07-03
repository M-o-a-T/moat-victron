#! /bin/sh

. /opt/victronenergy/serial-starter/run-service.sh

d=$(cd $(dirname $0) ; /bin/pwd)
app=$d/dbus-modbus-client.py

export PYTHONPATH=$d/serial/twe_meter:/opt/victronenergy/dbus-modbus-client/

start -x -s $tty -r 9600
