# ModBus TCP proxy

[![ModBus proxy][pypi-version]](https://pypi.python.org/pypi/modbus-proxy)
[![Python Versions][pypi-python-versions]](https://pypi.python.org/pypi/modbus-proxy)
[![Pypi status][pypi-status]](https://pypi.python.org/pypi/modbus-proxy)
![License][license]
[![CI][CI]](https://github.com/tiagocoutinho/modbus-proxy/actions/workflows/ci.yml)

Many modbus devices support only one or very few clients. This proxy acts as a bridge between the client and the modbus device. It can be seen as a
layer 7 reverse proxy.
This allows multiple clients to communicate with the same modbus device.

When multiple clients are connected, cross messages are avoided by serializing communication on a first come first served REQ/REP basis.

## Installation

From within your favorite python 3 environment type:

`$ pip install modbus-proxy`

Note: On some systems `pip` points to a python 2 installation.
You might need to use `pip3` command instead.

Additionally, if you want logging configuration:
* YAML: `pip install modbus-proxy[yaml]` (see below)
* TOML: `pip install modbus-proxy[toml]` (see below)

## Running the server

First, you will need write a configuration file where you specify for each modbus device you which to control:

* modbus connection (the modbus device url)
* listen interface (to which url your clients should connect)

Configuration files can be written in YAML (*.yml* or *.yaml*) or TOML (*.toml*).

Suppose you have a PLC modbus device listening on *plc1.acme.org:502* and you want your clients to
connect to your machine on port 9000. A YAML configuration would look like this:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502     # device url (mandatory)
    timeout: 10                # communication timeout (s) (optional, default: 10)
    connection_time: 0.1       # delay after connection (s) (optional, default: 0)
  listen:
    bind: 0:9000               # listening address (mandatory)
  unit_id_remapping:           # remap/forward unit IDs (optional, empty by default)
    1: 0
```

Assuming you saved this file as `modbus-config.yml`, start the server with:

```bash
$ modbus-proxy -c ./modbus-config.yml
```

Now, instead of connecting your client(s) to `plc1.acme.org:502` you just need to
tell them to connect to `*machine*:9000` (where *machine* is the host where
modbus-proxy is running).

Note that the server is capable of handling multiple modbus devices. Here is a
configuration example for 2 devices:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502
  listen:
    bind: 0:9000
- modbus:
    url: plc2.acme.org:502
  listen:
    bind: 0:9001
```

If you have a *single* modbus device, you can avoid writting a configuration file by
providing all arguments in the command line:

```bash
modbus-proxy -b tcp://0:9000 --modbus tcp://plc1.acme.org:502
```

For RTU-over-TCP devices (such as Elfin EE11 gateways), use the `rtu+tcp://` scheme:

```bash
modbus-proxy -b tcp://0:9000 --modbus rtu+tcp://192.168.42.162:8899 --timeout 5
```

(hint: run `modbus-proxy --help` to see all available options)

### Forwarding Unit Identifiers

You can also forward one unit ID to another whilst proxying. This is handy if the target
modbus server has a unit on an index that is not supported by one of your clients.

```yaml
devices:
- modbus: ... # see above.
  listen: ... # see above.
  unit_id_remapping:
    1: 0
```

The above forwards requests to unit ID 1 to your modbus-proxy server to unit ID 0 on the
actual modbus server.

Note that **the reverse also applies**: if you forward unit ID 1 to unit ID 0, **all** responses coming from unit 0 will look as if they are coming from 1, so this may pose problems if you want to use unit ID 0 for some clients and unit ID 1 for others (use unit ID 1 for all in that case).

## Running the examples

To run the examples you will need to have
[umodbus](https://github.com/AdvancedClimateSystems/uModbus) installed (do it
with `pip install umodbus`).

Start the `simple_tcp_server.py` (this will simulate an actual modbus hardware):

```bash
$ python examples/simple_tcp_server.py -b :5020
```

You can run the example client just to be sure direct communication works:

```bash
$ python examples/simple_tcp_client.py -a 0:5020
holding registers: [1, 2, 3, 4]
```

Now for the real test:

Start a modbus-proxy bridge server with:

```bash
$ modbus-proxy -b tcp://:9000 --modbus tcp://:5020
```

Finally run a the example client but now address the proxy instead of the server
(notice we are now using port *9000* and not *5020*):

```bash
$ python examples/simple_tcp_client.py -a 0:9000
holding registers: [1, 2, 3, 4]
```

## RTU-over-TCP Support

Modbus-proxy supports RTU-over-TCP connections for devices that communicate using Modbus RTU framing over TCP connections (such as RS-485 to TCP gateways like Elfin EE11).

### Key Features

* **Protocol Conversion**: Automatically converts between Modbus TCP (MBAP) and Modbus RTU framing
* **CRC Validation**: Validates CRC16 checksums in RTU responses
* **Function Code Support**: Supports standard Modbus functions (0x01-0x06, 0x0F, 0x10, 0x17) and exception responses
* **Connection Management**: Maintains a single persistent upstream connection with automatic reconnection
* **Error Handling**: Robust timeout and retry handling with distinction between network and protocol errors
* **Metrics**: Built-in request counters and error tracking for monitoring

### Configuration

Use the `rtu+tcp://` scheme for RTU-over-TCP devices:

```yaml
devices:
- modbus:
    url: rtu+tcp://192.168.42.162:8899  # RTU-over-TCP device
    timeout: 5                          # Connection timeout (seconds)
  listen:
    bind: 0:1502                        # Listen for TCP clients
```

### Command Line Usage

```bash
modbus-proxy --bind tcp://0.0.0.0:1502 --modbus rtu+tcp://192.168.42.162:8899 --timeout 5
```

### RTU-over-TCP Protocol Details

* **Request Conversion**: MBAP → UnitID + PDU + CRC16
* **Response Conversion**: UnitID + FunctionCode + Data + CRC16 → MBAP + UnitID + PDU
* **Framing**: Proper handling of RTU frame structure based on function codes
* **Exception Handling**: Supports Modbus exception responses (function code | 0x80)

### Compatible Devices

This feature is designed to work with:
* Elfin EE11 RS-485 to TCP gateways
* Any device that implements Modbus RTU over TCP without MBAP headers
* Industrial gateways that bridge RS-485 Modbus RTU to TCP

### Supported Function Codes

| Code | Function | Description |
|------|----------|-------------|
| 0x01 | Read Coils | Read discrete outputs |
| 0x02 | Read Discrete Inputs | Read discrete inputs |
| 0x03 | Read Holding Registers | Read holding registers |
| 0x04 | Read Input Registers | Read input registers |
| 0x05 | Write Single Coil | Write single discrete output |
| 0x06 | Write Single Register | Write single holding register |
| 0x0F | Write Multiple Coils | Write multiple discrete outputs |
| 0x10 | Write Multiple Registers | Write multiple holding registers |
| 0x17 | Read/Write Multiple Registers | Read/write multiple holding registers |
| 0x8x | Exception Responses | All exception responses supported |

### Monitoring and Troubleshooting

The RTU-over-TCP client exposes metrics for monitoring:

```python
# Access metrics programmatically
metrics = bridge.rtu_client.metrics
print(f"Total requests: {metrics['requests']}")
print(f"CRC errors: {metrics['crc_errors']}")
print(f"Timeouts: {metrics['timeouts']}")
print(f"Protocol errors: {metrics['protocol_errors']}")
```

**Common error patterns in logs:**

| Log Message | Cause | Solution |
|-------------|-------|----------|
| `Invalid CRC in RTU response` | Electrical noise on RS-485 | Check cabling, add termination resistors |
| `Unit ID mismatch` | Wrong unit_id or multi-drop bus | Verify unit_id configuration |
| `Unsupported function code` | Device uses custom functions | Contact device vendor |
| `network error, will retry` | Network timeout or connection drop | Check network stability, increase timeout |
| `connection appears closed, reconnecting` | Gateway closed idle connection | Normal behavior, no action needed |

### Performance Considerations

* **Latency**: Typical RTU-over-TCP round-trip: 50-200ms (depends on gateway and RS-485 bus speed)
* **Throughput**: ~5-10 requests/second per device (limited by RTU serial nature)
* **Concurrent Clients**: Multiple TCP clients can connect; requests are serialized upstream
* **Connection Reuse**: The proxy maintains one persistent connection to the RTU gateway

### Example Use Cases

1. **Home Assistant + Huawei Solar Inverter via Elfin EE11**
   ```bash
   modbus-proxy -b tcp://0:1502 --modbus rtu+tcp://elfin-ee11.local:8899 --timeout 5
   ```

2. **Multiple RTU devices on same RS-485 bus** (using unit_id)
   ```yaml
   devices:
   - modbus:
       url: rtu+tcp://192.168.1.100:8899
       timeout: 5
     listen:
       bind: 0:1502
     unit_id_remapping:
       1: 1  # Inverter 1
       2: 2  # Inverter 2
   ```

3. **Industrial PLC with RTU gateway**
   ```yaml
   devices:
   - modbus:
       url: rtu+tcp://plc-gateway:502
       timeout: 10  # Longer timeout for slow PLCs
     listen:
       bind: 0:5020
   ```

## Running as a Service
1. move the config file to a location you can remember, for example: to `/usr/lib/mproxy-conf.yaml`
2. go to `/etc/systemd/system/`
3. use nano or any other text editor of your choice to create a service file `mproxy.service`
4. the file should contain the following information:
```
[Unit]
Description=Modbus-Proxy
After=network.target

[Service]
Type=simple
Restart=always
ExecStart = modbus-proxy -c ./usr/lib/mproxy-conf.yaml

[Install]
WantedBy=multi-user.target
```
5. `run systemctl daemon-reload`
6. `systemctl enable mproxy.service`
7. `systemctl start mproxy.service`

The file names given here are examples, you can choose other names, if you wish.

## Docker

This project ships with a basic [Dockerfile](
./Dockerfile) which you can use
as a base to launch modbus-proxy inside a docker container.

First, build the docker image with:

```bash
$ docker build -t modbus-proxy .
```


To bridge a single modbus device without needing a configuration file is
straight forward:

```bash
$ docker run -d -p 5020:502 modbus-proxy -b tcp://0:502 --modbus tcp://plc1.acme.org:502
```

Now you should be able to access your modbus device through the modbus-proxy by
connecting your client(s) to `<your-hostname/ip>:5020`.

If, instead, you want to use a configuration file, you must mount the file so
it is visible by the container.

Assuming you have prepared a `conf.yml` in the current directory:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502
  listen:
    bind: 0:502
```

Here is an example of how to run the container:

```bash
docker run -p 5020:502 -v $PWD/conf.yml:/config/modbus-proxy.yml modbus-proxy
```

By default the Dockerfile will run `modbus-proxy -c /config/modbus-proxy.yml` so
if your mounting that volume you don't need to pass any arguments.

Note that for each modbus device you add in the configuration file you need
to publish the corresponding bind port on the host
(`-p <host port>:<container port>` argument).

## Logging configuration

Logging configuration can be added to the configuration file by adding a new `logging` keyword.

The logging configuration will be passed to
[logging.config.dictConfig()](https://docs.python.org/library/logging.config.html#logging.config.dictConfig)
so the file contents must obey the
[Configuration dictionary schema](https://docs.python.org/library/logging.config.html#configuration-dictionary-schema).

Here is a YAML example:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502
  listen:
    bind: 0:9000
logging:
  version: 1
  formatters:
    standard:
      format: "%(asctime)s %(levelname)8s %(name)s: %(message)s"
  handlers:
    console:
      class: logging.StreamHandler
      formatter: standard
  root:
    handlers: ['console']
    level: DEBUG
```

### `--log-config-file` (deprecated)

Logging configuration file.

If a relative path is given, it is relative to the current working directory.

If a `.conf` or `.ini` file is given, it is passed directly to
[logging.config.fileConfig()](https://docs.python.org/library/logging.config.html#logging.config.fileConfig) so the file contents must
obey the
[Configuration file format](https://docs.python.org/library/logging.config.html#configuration-file-format).

A simple logging configuration (also available at [log.conf](examples/log.conf))
which mimics the default configuration looks like this:

```toml
[formatters]
keys=standard

[handlers]
keys=console

[loggers]
keys=root

[formatter_standard]
format=%(asctime)s %(levelname)8s %(name)s: %(message)s

[handler_console]
class=StreamHandler
formatter=standard

[logger_root]
level=INFO
handlers=console
```

A more verbose example logging with a rotating file handler:
[log-verbose.conf](examples/log-verbose.conf)

The same example above (also available at [log.yml](examples/log.yml)) can be achieved in YAML with:

```yaml
version: 1
formatters:
  standard:
    format: "%(asctime)s %(levelname)8s %(name)s: %(message)s"
handlers:
  console:
    class: logging.StreamHandler
    formatter: standard
root:
  handlers: ['console']
  level: DEBUG
```

## Kubernetes

Based on the existing [Dockerfile](Dockerfile) an example Kubernetes manifest has been created and is located at [examples/kubernetes](examples/kubernetes).

* `ConfigMap` for modbus-proxy config: [`examples/kubernetes/10_modbus-proxy.configmap.yaml`](examples/kubernetes/10_modbus-proxy.configmap.yaml)

After the adjustment of the configmap, the manifest could get applied to your Kubernetes Cluster:
```
# adjust config
vim examples/kubernetes/10_modbus-proxy.configmap.yaml

# apply to cluster
kubectl apply -f examples/kubernetes
```

## Installing modbus-proxy on Raspberry PI VENV (virtual environment)

Create subfolder for all virtual environments and activate venv 'modbusproxy'
```bash
sudo mkdir python
cd python
python3 -m venv modbusproxy
source modbusproxy/bin/activate
```
It should look like this now:
```bash
(modbusproxy) pi@raspberrypi:~/python $
```

Install modbus-proxy and create config yaml
```bash
pip install modbus-proxy[yaml]
sudo nano modbus-config.yml
```

Update config file
```yaml
devices:
# Daheimlader
- modbus:
    url: 192.168.###.###:502
  listen:
    bind: 0:5001
# SolarEdge
- modbus:
    url: 192.168.###.###:1502
  listen:
    bind: 0:5002
```
Create autostart in systemd

```bash
cd /etc/systemd/system/
sudo nano mproxy.service
```

```
[Unit]
Description=Modbus-Proxy
After=network.target

[Service]
Type=simple
Restart=always
Environment="PATH=/home/pi/python/modbusproxy/bin"
ExecStart= /home/pi/python/modbusproxy/bin/modbus-proxy -c /home/pi/python/modbus-config.yml
user=pi

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable mproxy.service
sudo systemctl start mproxy.service
```

## Credits

### Development Lead

* Tiago Coutinho <coutinhotiago@gmail.com>

### Contributors

None yet. Why not be the first?

[pypi-python-versions]: https://img.shields.io/pypi/pyversions/modbus-proxy.svg
[pypi-version]: https://img.shields.io/pypi/v/modbus-proxy.svg
[pypi-status]: https://img.shields.io/pypi/status/modbus-proxy.svg
[license]: https://img.shields.io/pypi/l/modbus-proxy.svg
[CI]: https://github.com/tiagocoutinho/modbus-proxy/actions/workflows/ci.yml/badge.svg
