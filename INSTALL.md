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

### install the LiFePo4wered daemon

If you're using an external Raspberry Pi, you might wonder what to power it from
so that the system can do a cold start from battery if necessary.

The solution is a LiFePo4wered.com hat with its own battery cell. This add-on
accepts 12V power, so you can feed it from the MoaT BMS board.

The LiFePo4wered hat requires a daemon that tells it that the Pi is still alive,
otherwise it will shut down. (You can configure it to power-cycle the Pi instead.)

`git clone http://git.extern.smurf.noris.de/lifepo4wered.git/ ~/lifepo4`
`cd ~/lifepo4`
`make USE_SYSTEMD=0 PREFIX=/usr BUS=1 user-install`

Now power down the Raspberry Pi, install the LiFePo4wered hat, remove the Pi's
power supply, and hold the hat's button until your Pi powers on. The hat's green LED
should stop flashing eventually.

### edit the config file

#### batt.batt.n

The number of cells in your battery.

#### batt.cell.balance.min

The cell voltage above which balancing should start.

#### batt.cell.balance.d

The voltage difference between cells which triggers balancing. (Actually, balancing won't
start until the difference is twice this value.)

0.015 is a sensible value.

#### batt.cell.balance.n

The number of cells that can be balanced at the same time. Zero: all of them.

#### batt.cell.balance.r

Hysteresis ratio, so the balancer won't turn cells on and off all the time.

Sensible values are between zero and 0.3 or so.

#### batt.batt.i.min, .max

The battery's "hard" current limits. The protective relay will trip if these are exceeded.
Leave ample margin so that this won't happen during normal operation. Things tend to break
when disconnected under load.

#### batt.batt.i.ext min, .max

The battery's current limits as communicated to the rest of the Victron system.

These limits are reduced automatically when a cell approaches its voltage minimum / maximum.

#### batt.cell.u.min, .max

Your cell's "hard" voltage limts. The protective relay will trip if these are exceeded.
Leave ample margin so that this won't happen during normal operation.

#### batt.cell.u.ext.min, .max

The cell's operating range.

#### batt.cell.u.lim.min, .max

The voltage range which the BMS considers "safe", i.e. current limits are maxed when all
cells are within this range.

Note: The voltage limits reported to the Venus/GX system are not a simple multiple of the
cells' voltages. The BMS accounts for non-balanced cell voltages.

#### batt.batt.u.min, .max

Additional protection is provided by a voltage sensor on the BMS. It is not particularly accurate,
so you should probably set the minimum to somewhere between batt.cell.u.ext.min and batt.cell.u.min,
multiplied with the number of cells of course. Ditto for .max.

The BMS will tell the system to stop charging/discharging when the midpoint between this value
and the calculated safe minimum/maximum (based on cell limits) is crossed.

### setup the MoaT BMS

Change to `bus/python`. Copy `config/bms.cfg` and adapt to your battery, cells, etc..

Run `scripts/mpy-moat -vc CONFIG setup -C /usr/local/bin/mpy-cross -S once -s micro -c CONFIG` to

* copy the code to the RP2040
* tell it to run it once, i.e. it'll return to the input prompt after an error

Run `scripts/mpy-moat -vc CONFIG mplex -d`.

This command mostly ignores the input configuration file; the real configuration is stored
on the RP2040.

The first thing this does is to send an "Identify" command to all BMS modules, so the blue
LEDs should all light up and stay on for a bit. If they don't:

* check your wiring
* did you re-flash them?

If the chain ends somewhere, check for a loose cable or a mis-programmed controller.


### Calibrate the BMS

Start off by disengaging the battery relay:

`dbus -y com.victronenergy.battery.batt /bms/0 ForceRelay %False`

If you're using the battery to generate 12V, remove the DC converter's fuse.
(Assuming that its power passes through the current sensor.)

The command

`dbus -y com.victronenergy.battery.batt /bms/0 GetWork %True %True`

measures the energy going in and out of the battery. The numbers for "chg" and "dis"
should be identical, over time. If they are not, adjust the offset via the Dbus
'SetCurrentOffset' function.

Put the fuse back if you removed it.

Next, measure each cell's voltage, to calibrate the meters in the cells' controllers.

`dbus -y com.victronenergy.battery.batt /bms/0/NNN Identify`
`dbus -y com.victronenergy.battery.batt /bms/0/NNN SetVoltage %3.214`

Repeat this for x from 0 to the number of cells minus one.

Measure the battery voltage and set the controller's overall voltage to it:

`dbus -y com.victronenergy.battery.batt /bms/0 SetVoltage %25.67`

`dbus -y com.victronenergy.battery.batt /bms/0 GetVoltages`

Compare the "bms" and "cells" values. Hopefully they're reasonably identical.

Next, engage the relay:

`dbus -y com.victronenergy.battery.batt /bms/0 ForceRelay %True`

… and return control of the relay to the BMS:

`dbus -y com.victronenergy.battery.batt /bms/0 ReleaseRelay`

You can use `GetRelayState` to check the current state. This call returns two Boolean
values: the first states whether the relay is on and the second is `True` if the relay
state has been fixed via `ForceRelay`.

With the relay engaged, the solar charger should wake up. Configure it
(Bluetooth, VE Direct, VE Bus, …) and connect it to your Venus or GX.

Check the Victron GUI what the solar charger thinks the voltage is, and set that:
`
`dbus -y com.victronenergy.battery.batt /bms/0 SetExternalVoltage %26.37`


### Set up the Multiplus(es)

You need to enable the ESS assistant. How to do that is documented elsewhere.


### Set up Venus/GX

* Settings/ESS: set to "External Control".

* Settings/DVCC: turn on. 


### Tell the system what to do

The `inv\_control` script does the rest of the work. You might want to start with `-m analyze
-p top\_off 1` which top-balances your battery and then proceeds to measuring its capacity.

You should run `-m analyze` to measure the battery's real-use capacity. The BMS will then use
the result to display a reasonable SoC (State of Charge), as relying on cell voltage tends to
be notoriously wrong.

Normal use is `-m gridsetpoint`, which tells your inverter to hold your grid power at zero if possible.

