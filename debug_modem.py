#!/usr/bin/env python3
"""Modem diagnostic: zeigt Nummer, Speicher und alle SMS."""

import serial
import time

DEVICE = "/dev/ttyUSB2"


def at(modem, cmd, wait_for="OK", timeout=5.0):
    modem.write((cmd + "\r").encode())
    deadline = time.monotonic() + timeout
    buf = ""
    while time.monotonic() < deadline:
        buf += modem.read(modem.in_waiting or 1).decode(errors="replace")
        if wait_for in buf:
            return buf
    return "(timeout) " + buf


with serial.Serial(DEVICE, baudrate=115200, timeout=5) as m:
    print("=== Eigene Nummer ===")
    print(at(m, "AT+CNUM"))

    print("=== SMS Speicher (CPMS) ===")
    print(at(m, "AT+CPMS?"))

    print("=== Auf SM (SIM) umschalten und lesen ===")
    print(at(m, 'AT+CPMS="SM","SM","SM"'))
    at(m, "AT+CMGF=1")
    print(at(m, 'AT+CMGL="ALL"', timeout=10))

    print("=== Auf ME (Modem) umschalten und lesen ===")
    print(at(m, 'AT+CPMS="ME","ME","ME"'))
    print(at(m, 'AT+CMGL="ALL"', timeout=10))

    print("=== Zurück zu PDU-Modus ===")
    at(m, "AT+CMGF=0")
    print("Fertig.")
