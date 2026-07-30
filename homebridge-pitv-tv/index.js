'use strict';

// homebridge-pitv-tv
//
// Publishes a single HomeKit *Television* accessory for the Pi's HDMI-CEC TV:
//   - power on/off
//   - input switching (each input sends a raw CEC frame)
//   - remote-key navigation, so the Apple Home / Control Centre remote drives
//     the PiTV menu
//
// A Television MUST be published as an EXTERNAL accessory
// (api.publishExternalAccessories) — bridged accessories appear *inside* the
// Homebridge bridge, whereas an external accessory with category TELEVISION
// appears as its own TV tile. That's the fix for "shows up as a bridge".
//
// All actions are sent to tv_menu.py as a localhost UDP datagram
// (127.0.0.1:8129). The official Homebridge service is sandboxed
// (ProtectSystem=strict) so it can't write a /tmp FIFO, but it can always send
// a localhost packet. tv_menu.py owns the CEC bus and runs the commands itself.
//   TV_ON | TV_OFF   -> power
//   CEC <tx frame>   -> input switching
//   KEY <TOKEN>      -> menu navigation (UP/DOWN/LEFT/RIGHT/SELECT/BACK/HOME)

const dgram = require('dgram');

const PLUGIN_NAME   = 'homebridge-pitv-tv';
const PLATFORM_NAME = 'PiTVTelevision';
const CEC_UDP_HOST  = '127.0.0.1';
const CEC_UDP_PORT  = 8129;

// Default inputs match the frames from the known-working legacy config:
// "tx 4f:82:X0:00" = broadcast Active Source = physical address X.0.0.0.
const DEFAULT_INPUTS = [
  { name: 'HDMI 1', cec: 'tx 4f:82:10:00' },
  { name: 'HDMI 2', cec: 'tx 4f:82:20:00' },
];

let Service, Characteristic, Categories;

module.exports = (api) => {
  Service        = api.hap.Service;
  Characteristic = api.hap.Characteristic;
  Categories     = api.hap.Categories;
  api.registerPlatform(PLATFORM_NAME, PiTVTelevisionPlatform);
};

class PiTVTelevisionPlatform {
  constructor(log, config, api) {
    this.log    = log;
    this.config = config || {};
    this.api    = api;
    this.name   = this.config.name || 'TV';
    this.inputs = (Array.isArray(this.config.inputs) && this.config.inputs.length)
      ? this.config.inputs
      : DEFAULT_INPUTS;

    // CEC state can't be polled reliably (single-owner bus), so we remember
    // what HomeKit last set and report that back.
    this.active      = 0;   // Characteristic.Active.INACTIVE
    this.activeInput = 1;

    this.api.on('didFinishLaunching', () => this.publishTelevision());
  }

  // Required stub for platform plugins.
  configureAccessory() {}

  // Fire-and-forget a UDP datagram to tv_menu.py.
  send(str) {
    const client = dgram.createSocket('udp4');
    client.send(Buffer.from(str), CEC_UDP_PORT, CEC_UDP_HOST, (err) => {
      if (err) this.log.error(`UDP send "${str}" failed: ${err.message}`);
      else     this.log.info(`-> ${str}`);
      client.close();
    });
  }

  publishTelevision() {
    const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}:${this.name}`);
    const tv = new this.api.platformAccessory(
      this.name, uuid, Categories.TELEVISION,
    );

    tv.getService(Service.AccessoryInformation)
      .setCharacteristic(Characteristic.Manufacturer, 'PiTV')
      .setCharacteristic(Characteristic.Model, 'CEC Television')
      .setCharacteristic(Characteristic.SerialNumber, 'pitv-tv-1');

    const tvService = tv.addService(Service.Television);
    tvService
      .setCharacteristic(Characteristic.ConfiguredName, this.name)
      .setCharacteristic(
        Characteristic.SleepDiscoveryMode,
        Characteristic.SleepDiscoveryMode.ALWAYS_DISCOVERABLE,
      );

    // Power on/off — the control on the TV tile.
    tvService.getCharacteristic(Characteristic.Active)
      .onGet(() => this.active)
      .onSet((value) => {
        this.active = value;
        this.send(value ? 'TV_ON' : 'TV_OFF');
      });

    // Input switching. Selecting an input in the Home app sends its CEC frame.
    tvService.getCharacteristic(Characteristic.ActiveIdentifier)
      .onGet(() => this.activeInput)
      .onSet((id) => {
        this.activeInput = id;
        const inp = this.inputs[id - 1];
        if (inp && inp.cec) this.send(`CEC ${inp.cec}`);
      });
    tvService.setCharacteristic(Characteristic.ActiveIdentifier, this.activeInput);

    this.inputs.forEach((inp, i) => {
      const id  = i + 1;
      const src = tv.addService(Service.InputSource, `input${id}`, inp.name);
      src
        .setCharacteristic(Characteristic.Identifier, id)
        .setCharacteristic(Characteristic.Name, inp.name)
        .setCharacteristic(Characteristic.ConfiguredName, inp.name)
        .setCharacteristic(
          Characteristic.IsConfigured,
          Characteristic.IsConfigured.CONFIGURED,
        )
        .setCharacteristic(
          Characteristic.InputSourceType,
          Characteristic.InputSourceType.HDMI,
        )
        .setCharacteristic(
          Characteristic.CurrentVisibilityState,
          Characteristic.CurrentVisibilityState.SHOWN,
        )
        .setCharacteristic(
          Characteristic.TargetVisibilityState,
          Characteristic.TargetVisibilityState.SHOWN,
        );
      tvService.addLinkedService(src);
    });

    // Remote-key navigation. The Home app remote (and the Control Centre TV
    // remote) send these; we forward them to the PiTV menu as nav tokens.
    const RK = Characteristic.RemoteKey;
    const KEY_MAP = {
      [RK.ARROW_UP]:    'UP',
      [RK.ARROW_DOWN]:  'DOWN',
      [RK.ARROW_LEFT]:  'LEFT',
      [RK.ARROW_RIGHT]: 'RIGHT',
      [RK.SELECT]:      'SELECT',
      [RK.PLAY_PAUSE]:  'SELECT',
      [RK.BACK]:        'BACK',
      [RK.EXIT]:        'HOME',
      [RK.INFORMATION]: 'HOME',
    };
    tvService.getCharacteristic(RK).onSet((key) => {
      const tok = KEY_MAP[key];
      if (tok) this.send(`KEY ${tok}`);
    });

    // Publish EXTERNALLY so it becomes its own TV tile, not a bridge entry.
    this.api.publishExternalAccessories(PLUGIN_NAME, [tv]);
    this.log.info(`Published Television accessory "${this.name}" (external) `
      + `with ${this.inputs.length} input(s).`);
  }
}
