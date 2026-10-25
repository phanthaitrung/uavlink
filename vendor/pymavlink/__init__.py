'''Python MAVLink library - see https://mavlink.io/en/'''
__version__ = '2.4.50'

# uavlink: vendored copy ships only the MAVLink 2 ardupilotmega dialect; select it unless the caller chose another.
import os as _os
_os.environ.setdefault("MAVLINK20", "1")
_os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")
