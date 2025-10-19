# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""Tests for `modbus_proxy` package."""

import os
import json
import asyncio
from collections import namedtuple
from urllib.parse import urlparse
from tempfile import NamedTemporaryFile

import toml
import yaml
import pytest

from modbus_proxy import parse_url, parse_args, load_config, run, modbus_crc, append_crc, verify_crc

from .conftest import REQ, REP, REQ2, REP2, REQ3_ORIGINAL, REP3_MODIFIED


Args = namedtuple(
    "Args", "config_file bind modbus modbus_connection_time timeout"
)


CFG_YAML_TEXT = """\
devices:
- modbus:
    url: plc1.acme.org:502
  listen:
    bind: 0:9000
- modbus:
    url: plc2.acme.org:502
  listen:
    bind: 0:9001
"""

CFG_TOML_TEXT = """
[[devices]]

[devices.modbus]
url = "plc1.acme.org:502"
[devices.listen]
bind = "0:9000"
[[devices]]

[devices.modbus]
url = "plc2.acme.org:502"
[devices.listen]
bind = "0:9001"
"""

CFG_JSON_TEXT = """
{
  "devices": [
    {
      "modbus": {
        "url": "plc1.acme.org:502"
      },
      "listen": {
        "bind": "0:9000"
      }
    },
    {
      "modbus": {
        "url": "plc2.acme.org:502"
      },
      "listen": {
        "bind": "0:9001"
      }
    }
  ]
}
"""


class Ready(asyncio.Event):
    def set(self, data):
        self.data = data
        super().set()


@pytest.mark.parametrize(
    "url, expected",
    [
        ("tcp://host:502", urlparse("tcp://host:502")),
        ("host:502", urlparse("tcp://host:502")),
        ("tcp://:502", urlparse("tcp://0:502")),
        (":502", urlparse("tcp://0:502")),
    ],
    ids=["scheme://host:port", "host:port", "scheme://:port", ":port"],
)
def test_parse_url(url, expected):
    assert parse_url(url) == expected


@pytest.mark.parametrize(
    "args, expected",
    [
        (["-c", "conf.yml"], Args("conf.yml", None, None, 0, 10)),
        (["--config-file", "conf.yml"], Args("conf.yml", None, None, 0, 10)),
    ],
    ids=["-c", "--config-file"],
)
def test_parse_args(args, expected):
    result = parse_args(args)
    assert result.config_file == expected.config_file
    assert result.bind == expected.bind
    assert result.modbus == expected.modbus
    assert result.modbus_connection_time == expected.modbus_connection_time
    assert result.timeout == expected.timeout


@pytest.mark.parametrize(
    "text, parser, suffix",
    [
        (CFG_YAML_TEXT, yaml.safe_load, ".yml"),
        (CFG_TOML_TEXT, toml.loads, ".toml"),
        (CFG_JSON_TEXT, json.loads, ".json"),
    ],
    ids=["yaml", "toml", "json"],
)
def test_load_config(text, parser, suffix):
    with NamedTemporaryFile("w+", suffix=suffix, delete=False) as f:
        f.write(text)
    try:
        config = load_config(f.name)
    finally:
        os.remove(f.name)
    assert parser(text) == config


async def open_connection(modbus):
    return await asyncio.open_connection(*modbus.address)


async def make_requests(modbus, requests):
    reader, writer = await open_connection(modbus)
    for request, reply in requests:
        writer.write(request)
        await writer.drain()
        assert await reader.readexactly(len(reply)) == reply
    writer.close()
    await writer.wait_closed()


@pytest.mark.parametrize(
    "req, rep",
    [
        (REQ, REP),
        (REQ2, REP2),
        (REQ3_ORIGINAL, REP3_MODIFIED),
    ],
    ids=["req1", "req2", "req3"],
)
@pytest.mark.asyncio
async def test_modbus(modbus, req, rep):

    assert not modbus.opened

    await make_requests(modbus, [(req, rep)])

    assert modbus.opened

    # Don't make any request
    _, w = await open_connection(modbus)
    w.close()
    await w.wait_closed()
    await make_requests(modbus, [(req, rep)])

    # Don't wait for answer
    _, w = await open_connection(modbus)
    w.write(REQ)
    await w.drain()
    w.close()
    await w.wait_closed()
    await make_requests(modbus, [(req, rep)])


@pytest.mark.asyncio
async def test_concurrent_clients(modbus):
    task1 = asyncio.create_task(make_requests(modbus, 10 * [(REQ, REP)]))
    task2 = asyncio.create_task(make_requests(modbus, 12 * [(REQ2, REP2)]))
    await task1
    await task2


@pytest.mark.asyncio
async def test_concurrent_clients_with_misbihaved(modbus):
    task1 = asyncio.create_task(make_requests(modbus, 10 * [(REQ, REP)]))
    task2 = asyncio.create_task(make_requests(modbus, 12 * [(REQ2, REP2)]))

    async def misbihaved(n):
        for i in range(n):
            # Don't make any request
            _, writer = await open_connection(modbus)
            writer.close()
            await writer.wait_closed()
            await make_requests(modbus, [(REQ, REP)])

            # Don't wait for answer
            _, writer = await open_connection(modbus)
            writer.write(REQ2)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    task3 = asyncio.create_task(misbihaved(10))
    await task1
    await task2
    await task3


@pytest.mark.parametrize(
    "req, rep",
    [
        (REQ, REP),
        (REQ2, REP2),
    ],
    ids=["req1", "req2"],
)
@pytest.mark.asyncio
async def test_run(modbus_device, req, rep):
    addr = "{}:{}".format(*modbus_device.address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        await make_requests(modbus, [(req, rep)])
    finally:
        for bridge in ready.data:
            await bridge.stop()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_device_not_connected(modbus):
    modbus.device.close()
    await modbus.device.wait_closed()

    with pytest.raises(asyncio.IncompleteReadError):
        await make_requests(modbus, [(REQ, REP)])


# Tests for CRC functions
def test_modbus_crc():
    """Test CRC16 calculation with known test vectors."""
    # Test vector 1: CRC of 0x0103 should be 0x2140
    data1 = b'\x01\x03'
    assert modbus_crc(data1) == 0x2140

    # Test vector 2: CRC of 0x010300010004 should be 0xC915
    # Full frame: 01 03 00 01 00 04 15 C9 (CRC in little-endian)
    data2 = b'\x01\x03\x00\x01\x00\x04'
    assert modbus_crc(data2) == 0xC915

    # Test vector 3: Empty data should be 0xFFFF
    assert modbus_crc(b'') == 0xFFFF


def test_append_crc():
    """Test appending CRC to data."""
    # Test with known data
    data = b'\x01\x03\x00\x01\x00\x04'
    frame_with_crc = append_crc(data)
    expected_crc = 0xC915
    expected_frame = data + bytes([expected_crc & 0xFF, (expected_crc >> 8) & 0xFF])
    assert frame_with_crc == expected_frame
    # Verify it matches known complete frame
    assert frame_with_crc == b'\x01\x03\x00\x01\x00\x04\x15\xC9'


def test_verify_crc():
    """Test CRC verification."""
    # Valid frame
    data = b'\x01\x03\x00\x01\x00\x04'
    crc = modbus_crc(data)
    frame = data + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    assert verify_crc(frame) is True

    # Invalid frame (wrong CRC)
    invalid_frame = data + b'\x00\x00'
    assert verify_crc(invalid_frame) is False

    # Frame too short
    assert verify_crc(b'\x01') is False


def test_parse_url_rtu_tcp():
    """Test parsing of rtu+tcp URLs."""
    # Test rtu+tcp scheme
    url = parse_url("rtu+tcp://192.168.1.100:8899")
    assert url.scheme == "rtu+tcp"
    assert url.hostname == "192.168.1.100"
    assert url.port == 8899

    # Test default scheme addition still works
    url = parse_url("192.168.1.100:502")
    assert url.scheme == "tcp"
    assert url.hostname == "192.168.1.100"
    assert url.port == 502

    # Test invalid scheme
    with pytest.raises(ValueError, match="Unsupported URL scheme"):
        parse_url("http://example.com:502")
