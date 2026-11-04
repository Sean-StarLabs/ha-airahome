<div align="center">
  <img src="https://brands.home-assistant.io/airahome/icon.png" height="50px" alt="logo">
</div>

<h3 align="center">Ha-AiraHome</h3>

<div align="center">

  ![Status](https://img.shields.io/badge/status-active-success)
  ![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-41BDF5)
  ![HACS](https://img.shields.io/badge/HACS-Custom-blue)
  ![Privacy](https://img.shields.io/badge/data-local_only-important)
  [![GitHub Issues](https://img.shields.io/github/issues/invy55/ha-airahome)](https://github.com/invy55/ha-airahome/issues)
  [![GitHub Pull Requests](https://img.shields.io/github/issues-pr/invy55/ha-airahome)](https://github.com/invy55/ha-airahome/pulls)
  ![GitHub License](https://img.shields.io/github/license/invy55/ha-airahome)

</div>

<p align="center">A local Home Assistant integration for Aira heat pumps via Bluetooth Low Energy.</p>

---

## 🧐 About

**Ha-AiraHome** is a custom integration for [Home Assistant](https://www.home-assistant.io/) that brings your Aira heat pump into your smart home setup. It works entirely locally, with no cloud in the loop and no permanent account access required. Communication happens directly over **Bluetooth Low Energy (BLE)**, using the [PyAiraHome](https://github.com/invy55/pyairahome) library.

The goal is to give you real control over your heat pump from within Home Assistant: not just monitoring, but the ability to set temperatures, switch modes, trigger DHW boosts, and wire everything into your automations. If you've ever felt like your heat pump was missing something, now you can do something about it.

Once set up, you get:

- **Climate control** — zone heating setpoints and an explicit whole-pump heating/cooling mode select
- **Hot water control** — DHW target temperature, boost mode, scheduler awareness
- **Sensors** — temperatures, power, energy, COP, flow rates, compressor data, and more
- **Binary sensors** — connection status, active modes, alarms, circulator pumps
- **Controls** — pending Away start/end dates and DHW boost duration, with explicit activate/cancel buttons and a one-hour night mode button
- **Services** — set/clear Away mode, activate/deactivate DHW boost, activate night mode

Away dates and boost duration are local selections restored across restarts. Editing them does not change the heat pump. Press **Activate Away** or **Start DHW boost** to apply them. New selections default to today, three days later, and one hour. Away activation requires a start date of today or later and at least three days between dates; restored dates are validated again when activated.

**Clear Away** and **Cancel DHW boost** send their dedicated cancel commands. Night mode lasts one hour and has no early cancel command in the SDK. Buttons require a connected heat pump. Commands request a state refresh after acknowledgement; existing mode sensors report the device state. **DHW boost active** compares the reported boost end with the current time on each coordinator update and is unknown when schedule data is missing or malformed. A complete schedule list with no active boost reports off. Cancel boost is available only for a confirmed unexpired boost; Start boost is unavailable while that boost is active. Night activation requires the reported night mode boolean to be false. Services enforce the same checks. Away actions remain available while connected: the current Away enabled flag does not establish whether a future Away schedule exists. Zone climate entities control heating setpoints only; use the Whole pump heating/cooling mode select for pump-wide permissions. Hot water controls retain scheduler ownership.

For the full list of available entities, see the [Features & Entities](https://github.com/Invy55/ha-airahome/wiki/Features-and-Entities) wiki page.

---

## 🏁 Installation

> [!TIP]
> **Before jumping straight to installation**, it's worth taking a few minutes to read the [Getting Started](https://github.com/Invy55/ha-airahome/wiki/Getting-started) and [Installation](https://github.com/Invy55/ha-airahome/wiki/Installation) wiki pages. A quick read upfront can save a lot of troubleshooting later, especially around Bluetooth setup.

The recommended way to install is via **HACS**:

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=invy55&repository=ha-airahome&category=integration)

Once installed, Home Assistant should detect your heat pump automatically and prompt you to set it up. You can also add it manually:

<details>
<summary>Click here to show the manual installation button</summary>

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=airahome)

</details>

For alternative installation methods and troubleshooting, see the [Installation Guide](https://github.com/Invy55/ha-airahome/wiki/Installation).

---

## 📖 Documentation

The full documentation is available in the [Wiki](https://github.com/Invy55/ha-airahome/wiki):

- [Getting Started](https://github.com/Invy55/ha-airahome/wiki/Getting-started)
- [Installation](https://github.com/Invy55/ha-airahome/wiki/Installation)
- [Features & Entities](https://github.com/Invy55/ha-airahome/wiki/Features-and-Entities)
- [HA is Too Far from Aira](https://github.com/Invy55/ha-airahome/wiki/HA-is-too-far-from-Aira) — Bluetooth proxy setup and adapter tips
- [Bluetooth Issues](https://github.com/Invy55/ha-airahome/wiki/Bluetooth-Issues) — troubleshooting guide

---

## 📋 Todo

Suggestions and contributions are welcome. For a quicker back-and-forth, bring your idea to the [forum](https://airausersforum.com) first. For confirmed bugs or feature requests, open an issue or pull request here.

### Open
- [ ] Add more actions (refresh saved configuration, ...)
- [ ] Add support for more sensors and data points (current ones are listed in [Features & Entities](https://github.com/Invy55/ha-airahome/wiki/Features-and-Entities))

### Future / Not currently possible
- [ ] Write Not Permitted ([#10](https://github.com/Invy55/ha-airahome/issues/10)): if you're hitting this, see the [Bluetooth Issues](https://github.com/Invy55/ha-airahome/wiki/Bluetooth-Issues) guide
- [ ] Add support for solar plants

---

## 🌐 Community

Questions, setup sharing, and feature ideas all live on the [Aira Users Forum](https://airausersforum.com). Come and join us. Someone has probably been there before you.

Confirmed bugs and feature requests can be filed on [GitHub Issues](https://github.com/invy55/ha-airahome/issues).

---

## 🌍 Translators

Thanks to everyone who contributed translations:

| Language | Contributors |
|----------|-------------|
| 🇬🇧 English | [@Invy55](https://github.com/Invy55), [@jam3sward](https://github.com/jam3sward) |
| 🇮🇹 Italian | [@Invy55](https://github.com/Invy55) |
| 🇩🇪 German | [@dbhorst](https://github.com/dbhorst) |

---

## ☕ Support Me

I created and currently maintain this project because I genuinely enjoy doing so. No need to tip, but if you'd still like to show some appreciation, you can do it here. Thank you!

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/Y8Y01NUQV3)

---

## ⚠️ Disclaimer

**Ha-AiraHome** is an independent, open-source project developed for interacting with Aira Home heat pumps via their app gRPC APIs and Bluetooth Low Energy protocols. This project is **not affiliated with, endorsed by, sponsored by, or associated with** Aira Home or any of its subsidiaries, affiliates, or partners.

### Important Legal Notice

- 🔒 This project is **not an official product** of Aira Home
- ⚖️ Use of this integration does **not imply any compatibility, support, or approval** from Aira Home
- 🏷️ All trademarks, service marks, and company names mentioned herein are the **property of their respective owners**
- ⚠️ **Use of this integration is at your own risk** — I'm not responsible for any damages, malfunctions, warranty voids, or issues arising from its use
- 🛡️ This software is provided **"AS IS"** without warranty of any kind, express or implied
- 🔍 No proprietary code, trade secrets, or copyrighted materials from Aira Home have been used in the development of this integration

**By using this integration, you acknowledge that you understand and accept these terms and any associated risks.**
