--- /tmp/dbm	2022-08-22 20:42:06.623656410 +0200
+++ /opt/victronenergy/dbus-modbus-client/dbus-modbus-client.py	2022-08-22 20:42:35.187656399 +0200
@@ -23,6 +23,15 @@
 import ev_charger
 import smappee
 
+import sys
+sys.path.insert(0,"/data/moat/serial/twe_meter")
+import TWE_ABB_B2x
+import TWE_EM24RTU
+import TWE_Eastron_SDM120
+import TWE_Eastron_SDM630v1
+import TWE_Eastron_SDM630v2
+import TWE_Eastron_SDM72D
+
 import logging
 log = logging.getLogger()
 
