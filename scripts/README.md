# Scripts for Venus

## Running Debian and Venus on the same machine

### build_image

Use this script on your desktop machine to build a combination Venus+Debian image.

Usage: `build_image DEB_IMG VENUS_IMG DEST_DEVICE"

Tested with a Raspberry Pi 3, might need slight modifications for other Pi models.


### setupdeb.sh

Run this once on Venus to finalize setting up Debian.


### boot\_image

A small helper to change whichever subvolume is next booted from.


### recv\_image

Download a new version of Venus to 


## Venus

### set\_feed

A script for manual control of your Multiplus, when ESS is in use and external control is turned on.

The single argument is a number: watts to fetch from the grid (positive) / send to the grid (negative).

Runs until interrupted.


## MoaT

### setup.sh

Setup script that does a whole lot of interesting things. To be documented.

### setup2.sh

Second-stage setup script.


## On-line graphing

###  graph

Display interesting graphs in real time. To be documented further.


### webstuff-dl

Download helper to get the blobs for the Web browser.
