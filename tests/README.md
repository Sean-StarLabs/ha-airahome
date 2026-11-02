# Offline control tests

Use Python 3.14 with `requirements-test.txt`, then run `python -m pytest -q` from the repository root. The suite uses real Home Assistant entity platforms, registries, restore state and service dispatch, with mocked Aira transport. Internet sockets are disabled by pytest-socket; Unix sockets remain available for the event loop. No Home Assistant server, credentials, Bluetooth hardware or device actuation is used.

Coverage includes startup without control commands, restored local selections, all supported boost durations, Away validation and command payloads, clear/boost-cancel/night commands, unavailable controls, service target resolution, empty/error acknowledgements and refresh ordering, and reported boost expiry/time zones/unknown data.
