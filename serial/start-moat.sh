#! /bin/sh

d=$(cd $(dirname $0) ; /bin/pwd)

. /opt/victronenergy/serial-starter/run-service.sh
. $d/../env/bin/activate

app=$d/../bus/python/scripts/mpy-moat

start -v -c $d/moat.$tty.cfg -p /dev/$tty mplex
