#!/usr/bin/python
"""
    control - This is the main program for the VRSE payload.

    ** This script must be run with ~/RockSat2020 as the working directory
       with all of the directories (logs, data, video) and all of the files
       (config.ini) in place.

    Contributors:
        Andrew Bruckbauer
        Konstantin Zaremski

    Testing:
        The control program can be tested using the '--test' argument.
        Flat Sat (Default)      $ python control.py --test
        Flat Sat (Explicit)     $ python control.py --test flatsat
        Buttons                 $ python control.py --test buttons
        ** During any test routine shutdown is simulated.

    Functionality:
        This program controls all VRSE functionality including arm extension
        and retraction, camera recording, and telemetry based on timer events
        provided by the host spacecraft.

    Spacecraft Battery Bus Timer Events:
        ID      Time    Description & Action
        GSE     T-30s   Spacecraft power is turned on and the Pi running this
                        control script boots up, loads this script as a service,
                        and waits unitl TE-R is triggered.
        TE-R    T+85s   The first timer event and one of two redundant lines is
                        powered, triggering motor extension and starting the 
                        video recording on the 360 degree camera.
        Interim         Between TE-R and TE-1 the camera will record the high
                        resolution 360 degree video at flight apogee.
        TE-1    T+261s  The first official timer event, but second for the VRSE
                        payload is powered triggering arm retraction and transfer
                        of the lower resolution file back to the Raspberry Pi for
                        data redundancy and durability if the camera is lost or
                        damaged during re-entry.
        TE-2    T+330s  The final timer event for the VRSE payload, which will
                        trigger a sync of filesystems and proper shutdown of the
                        Pi and other equipment for re-entry.
"""

# Import dependencies
import sys
import configparser
from RPi import GPIO
import datetime
import board
import os
import subprocess
import asyncio
from adafruit_motorkit import MotorKit

# Unique
from logger import Logger
import usbcamctl
import persist

# Load configuration from config.ini
config = configparser.ConfigParser()
config.read('./config.ini')

# Configuration & setup tasks
TE_R = float(config['pinout']['TimerEventR'])                  # Spacecraft Battery Bus Timer Event (TE-R)
TE_1 = float(config['pinout']['TimerEvent1'])                  # Spacecraft Battery Bus Timer Event (TE-1)
TE_2 = float(config['pinout']['TimerEvent2'])                  # Spacecraft Battery Bus Timer Event (TE-2)
EXTEND_LIMIT = float(config['pinout']['ExtendLimitSwitch'])    # Arm Extension Limit Switch
RETRACT_LIMIT = float(config['pinout']['RetractLimitSwitch'])  # Arm Retraction Limit Switch

# Whether or not timer events are triggered by an external signal (flatsat, mission) or 
EXTERNAL_TRIGGER = True

# Set GPIO mode
GPIO.setmode(GPIO.BCM)
# MotorKit class
arm = MotorKit(i2c=board.I2C()).motor3

# Shorthand
def armExtended(): True if GPIO.input(EXTEND_LIMIT) == 0 else False
def armRetracted(): True if GPIO.input(RETRACT_LIMIT) == 0 else False
def TE(id):
    if id == "R": True if (GPIO.input(TE_R) == 1 and EXTERNAL_TRIGGER) or (GPIO.input(TE_R) == 0 and not EXTERNAL_TRIGGER) else False
    if id == "1": True if (GPIO.input(TE_1) == 1 and EXTERNAL_TRIGGER) or (GPIO.input(TE_1) == 0 and not EXTERNAL_TRIGGER) else False
    if id == "2": True if (GPIO.input(TE_2) == 1 and EXTERNAL_TRIGGER) or (GPIO.input(TE_2) == 0 and not EXTERNAL_TRIGGER) else False

# Extend arm motor control operations
async def extendArm():
    try:
        # If extend limit switch not hit, extend (positive throttle),
        # otherwise return True to signify extension
        if not armExtended():
            arm.throttle = 1
            asyncio.sleep(1)
            while arm.throttle == 1:
                # Once extend limit is hit, set throttle to 0 and return True to signify extension
                if armExtended():
                    arm.throttle = 0
                    return True
        else: return True
    except: return False

# Retract arm motor control operations
async def retractArm():
    try:
        # If extend limit switch not hit, retract (negative throttle),
        # otherwise return True to signify retraction
        if not armRetracted():
            arm.throttle = -1
            asyncio.sleep(1)
            while arm.throttle == -1:
                # Once retract limit is hit, set throttle to 0 and return True to signify retraction
                if armRetracted():
                    arm.throttle = 0
                    return True
        else: return True
    except: return False

# Main program method
async def main(testing):
    operating = True
    # Begin logging
    Log = Logger()
    Log.out("    V.R.S.E. Payload Control Program Started at system time: " + str(datetime.datetime.now().strftime("%Y-%m-%d T%H:%M:%S")) + ".")
    Log.out("    Operation Mode: " + "MISSION" if not testing else "TESTING")
    
    # Setup GPIO
    global EXTERNAL_TRIGGER
    if testing: EXTERNAL_TRIGGER = testing != "buttons"
    GPIO_TRIGGER_MODE = GPIO.PUD_DOWN if EXTERNAL_TRIGGER else GPIO.PUD_UP
    GPIO.setup(TE_R, GPIO.IN, pull_up_down=GPIO_TRIGGER_MODE)
    GPIO.setup(TE_1, GPIO.IN, pull_up_down=GPIO_TRIGGER_MODE)
    GPIO.setup(TE_2, GPIO.IN, pull_up_down=GPIO_TRIGGER_MODE)
    GPIO.setup(EXTEND_LIMIT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(RETRACT_LIMIT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    Log.out("GPIO Setup Complete.")
    
    # ** if inhibititor pin logic goes here
    
    # Disable motor throttle in case program crashed and was enabled
    arm.throttle = 0
    asyncio.sleep(1)
    
    # Load previous state
    currentState = await persist.read()
    if currentState: Log.out(f"Persisting state detected ({currentState}). Possible power failure has occurred.")
    else: Log.out("No persisting state was detected, proceeding with normal execution order.")
    
    # Begin recording sensor data (telemetry)
    if operating:
        #Log.out("Beginning sensor data collection and telemetry.")
        Log.out("Now listening to timer event signals.")
    # Main loop listening for timer events
    while operating:
        if not currentState or currentState == "TE-R":
            if TE("R"):
                currentState = "TE-R"
                await persist.set(currentState)
                Log.out("TE-R signal detected, beginning arm extension and video recording.")
                
                # Set up camera for recording 
                async def record():
                    recording = False
                    usbOff = await usbcamctl.usb(False)
                    if usbOff:
                        Log.out("  USB ports have been disabled.")
                        camPower = await usbcamctl.power(True)
                        if camPower:
                            Log.out("  Camera has been sent the power on signal via. GPIO.")
                            recording = await usbcamctl.toggleRecord()
                            if recording: Log.out("  Camera recording has been triggered via. GPIO.")
                            else: Log.out("  Failed to trigger camera recording.")
                        else: Log.out("  Failed to send camera power signal.")
                    else: Log.out("  Failed to disable USB ports.")
                    return recording
                
                # Asynchronous tasks
                recording = asyncio.create_task(record())
                extension = asyncio.create_task(extendArm())
                status = {
                    "recording": await recording,
                    "extended": await extension
                }
                Log.out(f"The 360 degree camera is {'recording' if status['recording'] else 'not recording'}.")
                Log.out(f"The arm is {'extended' if status['extended'] else 'not extended'}.")
                
                # Move on to the next 
                currentState = "TE-1"
                await persist.set(currentState)
                Log.out("TE-R tasks are complete, waiting for TE-1 signal.")
        elif currentState == "TE-1":
            if TE("1"):
                Log.out("TE-1 signal detected, retracting arm and transferring low quality footage to Pi.")
                
                # Retract Arm
                retraction = await retractArm()
                Log.out(f"The arm is {'retracted' if retraction else 'not retracted'}.")

                # Stop recording and transfer files
                stoppedRecording = await usbcamctl.toggleRecord()
                Log.out(f"The 360 degree camera is {'no longer recording' if stoppedRecording else 'still recording'}.")
                usbOn = await usbcamctl.usb(True)
                Log.out(f"USB ports are {'now enabled' if usbOn else 'still disabled'}.")
                camOff = await usbcamctl.power(False)
                Log.out(f"The camera is {'shut down' if camOff else 'still running'}.")
                
                # Move on to the next 
                currentState = "TE-2"
                await persist.set(currentState)
        elif currentState == "TE-2":
            if TE("2"):
                Log.out("TE-2 signal detected, exiting signal listen mode and shutting down electronic systems.")
                currentState = "SPLASH"
                await persist.set(currentState)
                operating = False
        elif currentState == "SPLASH":
            Log.out("SPLASH state, exiting signal listen mode and shutting down electronic systems.")
            operating = False
    # ** Stop telemetry here
    # Sync to the drives & poweroff
    os.system("sync")
    
    # End logging
    Log.close()
    
    if not testing:
        os.system("sudo poweroff")

# Entry point
if __name__ == "__main__":
    # Parse arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "--test":
            # If more than two arguments 
            if len(sys.argv) > 2 and sys.argv[2] == "buttons":
                print("MODE: TESTING with buttons")
                asyncio.run(main("buttons"))
            else:
                print("MODE: TESTING FLATSAT")
                asyncio.run(main("flatsat"))
        else:
            print("MODE: MISSION")
            asyncio.run(main())
    else:
        print("MODE: MISSION")
        asyncio.run(main())
