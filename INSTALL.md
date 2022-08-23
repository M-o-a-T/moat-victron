# Installation of a MoaT/Victron system

## Prerequisites

This system has been tested with

* a Raspberry Pi 3 (or 4 or 2)

* a bunch of Victron Energy solar chargers

* one or three Victron Multiplus inverters (the latter in a 3-phase setup)

* the MoaT BMS

* a LiFePo4wered power supply plus battery for the Raspberry Pi

* some way to get 12V from your battery if it's larger (hint: it should be!)

## Steps

### Create a Venus image

I strongly recommend using the Debian kernel, not the kernel Venus ships.
The reason: you get all kernel modules and overlays you might want. In
particular you do need I²C for the LiFePowered battery.

Use `scripts/boot_image`.

### insert into Pi, connect keyboard, HDMI and LAN, boot it

You'll end up with a root shell.

### Set root password, disable firewall, log in via SSH

Type `passwd root` and select a suitable password.

Type `iptables -F`.

You now can talk to the Pi with SSH. Type `ip addr` to get the IP address.

### Select superuser mode

The firewall is off, so you can use your Web browser to connect to Venus.

Turn on Superuser mode and "SSH on LAN".

You now can disconnect the terminal.

### Clone this repo

`opkg update`
`opkg install git`
`cd /data`
`git clone git@github.com:M-o-a-T/moat-victron.git moat`
# alternately if you don't have a github accont
`git clone https://github.com/M-o-a-T/moat-victron.git moat`
`cd moat`
`bash scripts/setup.sh`

The setup script does a lot:

* update opkg
* install some necesary and/or just plain convenient packages
* download some submodules
* set up a Python virtual environment
* fetch required Python modules
* add udev rules for a Modbus-RTU interface, and Micropython/MoaT
* apply some patches to DVCC
* fetch btrfs into the Debian repo
* add a script as /usr/local/bin/btrfs that calls Debian's btrfs

This script can be used to run any other Debian program; just copy or link it.
Note that calling from Debian back into Venus requires clearing the environment
variables that are set in this script.

