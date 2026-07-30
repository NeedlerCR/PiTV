'use strict';

// homebridge-pitv-tv
//
// Publishes:
//   - a HomeKit *Television* (external accessory, so it shows as a TV tile,
//     not inside the bridge): power, input switching, and remote-key
//     navigation of the PiTV menu.
//   - a *Kill Switch* (bridged Switch): while on, tv_menu keeps forcing the
//     TV off; turning it off stops that.
//
// Everything is sent to tv_menu.py as a localhost UDP datagram
// (127.0.0.1:8129). The official Homebridge service is sandboxed
// (ProtectSystem=strict) so it can't write a /tmp FIFO, but it can always send
// a localhost packet. tv_menu.py owns the CEC bus and runs the commands.
//   TV_ON | TV_OFF        -> power
//   CEC <tx frame>        -> input switching
//   KEY <TOKEN>           -> menu navigation
//   KILL_ON | KILL_OFF    -> kill switch

const dgram = require('dgram');

const PLUGIN_NAME   = 'homebridge-pitv-tv';
const PLATFORM_NAME = 'PiTVTelevision';
const CEC_UDP_HOST  = '127.0.0.1';
const CEC_UDP_PORT  = 8129;

// Broadcast "Active Source = <physical address>" so the TV switches input.
// Initiator "1" = the Pi's CEC logical address (libcec registers as Recorder 1
// / LA 1 — confirmed by `cec-client 'scan'`); a mismatched initiator makes the
// kernel reject the frame with EINVAL. Sky sits at 1.0.0.0 (HDMI 1), the Pi at
// 2.0.0.0 (HDMI 2).
const DEFAULT_INPUTS = [
  { name: 'Sky',  cec: 'tx 1f:82:10:00' },   // HDMI 1
  { name: 'PiTV', cec: 'tx 1f:82:20:00' },   // HDMI 2
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
    this.log         = log;
    this.config      = config || {};
    this.api         = api;
    this.name        = this.config.name || 'TV';
    this.inputs      = (Array.isArray(this.config.inputs) && this.config.inputs.length)
      ? this.config.inputs
      : DEFAULT_INPUTS;
    this.accessories = [];   // cached bridged accessories (the kill switch)

    // CEC state can't be polled reliably (single-owner bus), so we remember
    // what HomeKit last set and report that back.
    this.active      = 0;    // Characteristic.Active.INACTIVE
    this.activeInput = 1;
    this.killOn      = false;

    this.api.on('didFinishLaunching', () => {
      this.publishTelevision();
      this.ensureKillSwitch();
    });
  }

  // Restore cached bridged accessories (kill switch) across restarts.
  configureAccessory(accessory) {
    this.accessories.push(accessory);
  }

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

    // Input switching. Selecting an input sends its CEC frame.
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
      [RK.SELECT]:      'SELECT',   // tap → Enter (menu/select)
      [RK.PLAY_PAUSE]:  'PLAY',     // play/pause → Space (fire/start)
      [RK.BACK]:        'BACK',
      [RK.EXIT]:        'HOME',
      [RK.INFORMATION]: 'SPEED',    // "i" → speed up (Snake)
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

  // Bridged Switch: while on, tv_menu keeps the TV forced off.
  ensureKillSwitch() {
    const name = `${this.name} Kill Switch`;
    const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}:killswitch`);
    let acc = this.accessories.find((a) => a.UUID === uuid);
    if (!acc) {
      acc = new this.api.platformAccessory(name, uuid);
      acc.addService(Service.Switch, name);
      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, [acc]);
      this.accessories.push(acc);
    }
    const svc = acc.getService(Service.Switch)
      || acc.addService(Service.Switch, name);
    svc.getCharacteristic(Characteristic.On)
      .onGet(() => this.killOn)
      .onSet((value) => {
        this.killOn = value;
        this.send(value ? 'KILL_ON' : 'KILL_OFF');
      });
    this.log.info(`Kill switch "${name}" ready.`);
  }
}
