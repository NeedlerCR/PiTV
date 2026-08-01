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
const fs    = require('fs');

const PLUGIN_NAME   = 'homebridge-pitv-tv';
const PLATFORM_NAME = 'PiTVTelevision';
const CEC_UDP_HOST  = '127.0.0.1';
const CEC_UDP_PORT  = 8129;
// tv_menu.py writes the TV's real power state here (updated from CEC, so it
// reflects the physical remote too). We read it to keep the Home tile in sync.
const TV_STATE_FILE = '/tmp/pitv-tv-state';
// tv_menu.py writes the TV's active input (CEC physical address like "10:00")
// here, so the Home app's input selection reflects reality.
const TV_INPUT_FILE = '/tmp/pitv-tv-input';

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
    this.guestOn     = false;

    this.api.on('didFinishLaunching', () => {
      this.publishTelevision();
      this.ensureKillSwitch();
      this.ensureGuestSwitch();
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

    // Power on/off — the control on the TV tile. onGet prefers the real CEC
    // state (which also catches the physical remote); a poll pushes external
    // changes so the tile updates on its own.
    const readPower = () => {
      try {
        const s = fs.readFileSync(TV_STATE_FILE, 'utf8').trim();
        return s === 'on' ? 1 : s === 'off' ? 0 : null;
      } catch (e) { return null; }
    };
    const activeChar = tvService.getCharacteristic(Characteristic.Active)
      .onGet(() => { const p = readPower(); return p === null ? this.active : p; })
      .onSet((value) => {
        this.active = value;
        this.send(value ? 'TV_ON' : 'TV_OFF');
      });
    setInterval(() => {
      const p = readPower();
      if (p !== null && p !== this.active) {
        this.active = p;
        activeChar.updateValue(p);
        this.log.info(`TV power changed externally -> ${p ? 'ON' : 'OFF'}`);
      }
    }, 4000);

    // Input switching. Selecting an input sends its CEC frame; onGet/poll
    // track the TV's REAL active input (from CEC) so re-selecting works and the
    // tile doesn't drift out of sync.
    const physOf = (cec) => {
      const m = /82:([0-9a-f]{2}:[0-9a-f]{2})/i.exec(cec || '');
      return m ? m[1].toLowerCase() : null;
    };
    const readInput = () => {
      let phys;
      try { phys = fs.readFileSync(TV_INPUT_FILE, 'utf8').trim().toLowerCase(); }
      catch (e) { return null; }
      const idx = this.inputs.findIndex((inp) => physOf(inp.cec) === phys);
      return idx >= 0 ? idx + 1 : null;
    };
    const activeIdChar = tvService.getCharacteristic(Characteristic.ActiveIdentifier)
      .onGet(() => { const i = readInput(); return i === null ? this.activeInput : i; })
      .onSet((id) => {
        this.activeInput = id;
        const inp = this.inputs[id - 1];
        if (inp && inp.cec) this.send(`CEC ${inp.cec}`);
      });
    tvService.setCharacteristic(Characteristic.ActiveIdentifier, this.activeInput);
    setInterval(() => {
      const i = readInput();
      if (i !== null && i !== this.activeInput) {
        this.activeInput = i;
        activeIdChar.updateValue(i);
        this.log.info(`TV input changed externally -> ${i}`);
      }
    }, 4000);

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

  // Bridged Switch: gates the guest web portal (on :8080).
  ensureGuestSwitch() {
    const name = 'Guest Mode';
    const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}:guestmode`);
    let acc = this.accessories.find((a) => a.UUID === uuid);
    if (!acc) {
      acc = new this.api.platformAccessory(name, uuid);
      acc.addService(Service.Switch, name);
      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, [acc]);
      this.accessories.push(acc);
    }
    const svc = acc.getService(Service.Switch)
      || acc.addService(Service.Switch, name);
    const readGuest = () => {
      try { return fs.readFileSync('/tmp/pitv-guest-mode', 'utf8').trim() === 'on'; }
      catch (e) { return null; }
    };
    const gsChar = svc.getCharacteristic(Characteristic.On)
      .onGet(() => { const g = readGuest(); return g === null ? this.guestOn : g; })
      .onSet((value) => {
        this.guestOn = value;
        this.send(value ? 'GUEST_ON' : 'GUEST_OFF');
      });
    // Reflect the admin web portal's toggle back into Home.
    setInterval(() => {
      const g = readGuest();
      if (g !== null && g !== this.guestOn) {
        this.guestOn = g;
        gsChar.updateValue(g);
      }
    }, 4000);
    this.log.info(`Guest Mode switch ready.`);
  }
}
