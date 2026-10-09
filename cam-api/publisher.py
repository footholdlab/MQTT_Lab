#!/usr/bin/env python3
"""Fake IoT publisher for the lab.

Simulates a home-automation device that produces realistic MQTT traffic, so the
enumeration and detection articles have something to show. The default device is
an IP camera integration that -- as sloppy real-world setups do -- publishes its
RTSP stream URL with embedded credentials in a RETAINED config topic. That is the
hook that turns an open broker into a foothold. All secrets are fake lab values.

The device is pluggable: add a Device subclass, register it in DEVICES, and
select it with the DEVICE_TYPE env var. Nothing else has to change.
"""
import json
import os
import random
import signal
import time
from dataclasses import dataclass

import paho.mqtt.client as mqtt


# --------------------------------------------------------------------------
# Configuration (from the environment, set by compose from .env)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    host: str = os.environ.get("MQTT_HOST", "mosquitto")
    port: int = int(os.environ.get("MQTT_PORT", "1883"))
    client_id: str = os.environ.get("MQTT_CLIENT_ID", "cam-api-01")
    username: str | None = os.environ.get("MQTT_USERNAME") or None
    password: str | None = os.environ.get("MQTT_PASSWORD") or None
    prefix: str = os.environ.get("TOPIC_PREFIX", "cam")
    interval: int = int(os.environ.get("PUBLISH_INTERVAL", "5"))
    device_type: str = os.environ.get("DEVICE_TYPE", "camera")   
    retain: bool = os.environ.get("CAM_RETAIN", "1") == "1"      ### Lab-02/03: Retain switch
# Added for TLS lab
    tls: bool = os.environ.get("MQTT_TLS", "") == "1"
    cafile: str | None = os.environ.get("MQTT_CAFILE") or None
    certfile: str | None = os.environ.get("MQTT_CERTFILE") or None
    keyfile: str | None = os.environ.get("MQTT_KEYFILE") or None


CONFIG = Config()


# --------------------------------------------------------------------------
# One MQTT message. Keeps topic/payload/qos/retain together so devices just
# return lists of these and the transport layer stays generic.
# --------------------------------------------------------------------------
@dataclass
class Message:
    topic: str
    payload: str
    qos: int = 0
    retain: bool = False


# --------------------------------------------------------------------------
# Devices. A device produces three kinds of message:
#   announce()   -> retained messages sent once on connect (config, status)
#   telemetry()  -> fresh messages sent every cycle
#   offline()    -> retained availability flip (also used as the last will)
# To add a new device type, subclass Device and register it in DEVICES.
# --------------------------------------------------------------------------
class Device:
    name = "device"

    def __init__(self, prefix: str):
        self.base = f"{prefix}/{self.name}"

    def announce(self) -> list[Message]:
        return []

    def telemetry(self) -> list[Message]:
        return []

    def offline(self) -> list[Message]:
        return [Message(f"{self.base}/status", "offline", qos=1, retain=CONFIG.retain)]   ### Lab-02/03: retain will 


class CameraDevice(Device):
    """IP camera integration.

    Realistic bad pattern: it publishes the camera's RTSP stream URL -- with the
    credentials embedded -- in a RETAINED config topic. Reading the broker hands
    an attacker the live feed and often-reused credentials. Fake lab values;
    override the CAM_RTSP_* env vars to point somewhere else.
    """
    name = "frontdoor"

    RTSP_HOST = os.environ.get("CAM_RTSP_HOST", "192.168.183.50")
    RTSP_USER = os.environ.get("CAM_RTSP_USER", "admin")
    RTSP_PASSWORD = os.environ.get("CAM_RTSP_PASSWORD", "lab-fake-cam-pass")
    RTSP_PATH = os.environ.get("CAM_RTSP_PATH", "Streaming/Channels/101")

    def _rtsp_url(self) -> str:
        return (f"rtsp://{self.RTSP_USER}:{self.RTSP_PASSWORD}"
                f"@{self.RTSP_HOST}:554/{self.RTSP_PATH}")

    def announce(self) -> list[Message]:
        config = {
            "name": "Front Door",
            "model": "LabCam 1000",
            "stream_url": self._rtsp_url(),   # leaked credentials live here
        }
        return [
            Message(f"{self.base}/config", json.dumps(config), qos=1, retain=CONFIG.retain),   ### Lab-02/03: retain 
            Message(f"{self.base}/status", "online", qos=1, retain=CONFIG.retain),             ### Lab-02/03: retain
        ]

    def telemetry(self) -> list[Message]:
        event = {
            "event": "motion" if random.random() < 0.3 else "idle",
            "ts": int(time.time()),
        }
        return [Message(f"{self.base}/events", json.dumps(event), qos=0)]


# Registry: DEVICE_TYPE -> Device class.
DEVICES = {
    "camera": CameraDevice,
}


def make_device(config: Config) -> Device:
    """Build the device selected by DEVICE_TYPE."""
    try:
        return DEVICES[config.device_type](config.prefix)
    except KeyError:
        raise SystemExit(f"unknown DEVICE_TYPE: {config.device_type!r}")


# --------------------------------------------------------------------------
# MQTT transport
# --------------------------------------------------------------------------
def publish(client: mqtt.Client, messages: list[Message]) -> None:
    """Send a batch of messages."""
    for m in messages:
        client.publish(m.topic, m.payload, qos=m.qos, retain=m.retain)


def on_connect(client, userdata, flags, reason_code, properties):
    """Announce the device once connected. userdata is the Device."""
    if reason_code == 0:
        print(f"connected as {CONFIG.client_id}", flush=True)
        publish(client, userdata.announce())
    else:
        print(f"connect failed: {reason_code}", flush=True)


def on_disconnect(client, userdata, flags, reason_code, properties):
    """Log the disconnect; paho handles the automatic reconnect."""
    print(f"disconnected: {reason_code}", flush=True)


def build_client(config: Config, device: Device) -> mqtt.Client:
    """Create and configure the MQTT client (paho v2 callback API)."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id)
# if block changed to work with TLS configuration
    if config.tls:                                                   ### Lab-03
        client.tls_set(ca_certs=config.cafile,                       ### Lab-03
                       certfile=config.certfile,                     ### Lab-03
                       keyfile=config.keyfile)                       ### Lab-03
    if config.username:
        client.username_pw_set(config.username, config.password)
    # Last will mirrors a clean offline, so subscribers always see device state.
    will = device.offline()[0]
    client.will_set(will.topic, will.payload, qos=will.qos, retain=will.retain)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.user_data_set(device)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
running = True


def stop(signum, frame):
    """Stop the loop on SIGTERM/SIGINT (docker stop)."""
    global running
    running = False


def main():
    device = make_device(CONFIG)
    client = build_client(CONFIG, device)

    # Retry the initial connection until the broker is reachable.
    while True:
        try:
            client.connect(CONFIG.host, CONFIG.port, keepalive=60)
            break
        except OSError as exc:
            print(f"broker not ready ({exc}); retrying in 2s", flush=True)
            time.sleep(2)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    client.loop_start()
    while running:
        publish(client, device.telemetry())
        time.sleep(CONFIG.interval)

    # Clean shutdown: flip the retained status to offline and disconnect.
    publish(client, device.offline())
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()

