'use strict';

// homebridge-pitv-tv
//
// Publishes a single HomeKit *Television* accessory whose power on/off is
// wired to the Pi's HDMI-CEC via /opt/pitv/cec-cmd.sh.
//
// The important detail — and the reason a TV normally shows up wrong in the
// Home app — is that a Television MUST be published as an EXTERNAL accessory
// (api.publishExternalAccessories). Bridged accessories appear *inside* the
// Homebridge bridge; an external accessory with category TELEVISION appears
// as its own TV tile. HomeKit also allows only one TV per bridge, so external
// publishing is the correct, supported approach.

const { execFile } = require('child_process');

const PLUGIN_NAME   = 'homebridge-pitv-tv';
const PLATFORM_NAME = 'PiTVTelevision';
// We send power commands to tv_menu.py's FIFO rather than calling cec-cmd.sh
// directly: tv_menu.py owns the CEC bus and runs the command itself, so this
// works no matter which user Homebridge runs as. The write is timeout-guarded
// so Homebridge never hangs if tv_menu isn't running.
const FIFO = '/tmp/tv_menu.fifo';

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

    // CEC power state can't be polled reliably (the bus is single-owner), so
    // we remember what HomeKit last set and report that back.
    this.active = Characteristic.Active.INACTIVE;

    // Publish once Homebridge has finished starting up.
    this.api.on('didFinishLaunching', () => this.publishTelevision());
  }

  // Required stub for platform plugins. We publish the TV fresh as an external
  // accessory each launch, so there is no cached accessory to restore.
  configureAccessory() {}

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

    // Power on/off — this is the control HomeKit shows on the TV tile.
    tvService.getCharacteristic(Characteristic.Active)
      .onGet(() => this.active)
      .onSet((value) => {
        this.active = value;
        const token = value ? 'TV_ON' : 'TV_OFF';
        this.log.info(`HomeKit -> TV ${value ? 'ON' : 'OFF'} (${token} -> ${FIFO})`);
        execFile('timeout', ['3', 'sh', '-c', `printf '%s\\n' '${token}' > ${FIFO}`],
          (err) => {
            if (err) this.log.error(`Writing ${token} to ${FIFO} failed: ${err.message}`);
          });
      });

    // HomeKit wants a TV to expose at least one input source.
    tvService.setCharacteristic(Characteristic.ActiveIdentifier, 1);
    const input = tv.addService(Service.InputSource, 'hdmi', 'HDMI');
    input
      .setCharacteristic(Characteristic.Identifier, 1)
      .setCharacteristic(Characteristic.ConfiguredName, 'HDMI')
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
      );
    tvService.addLinkedService(input);

    // Publish EXTERNALLY so it becomes its own TV tile, not a bridge entry.
    this.api.publishExternalAccessories(PLUGIN_NAME, [tv]);
    this.log.info(`Published Television accessory "${this.name}" as an external accessory.`);
    this.log.info('Add it in the Home app via "Add Accessory" using the '
      + 'Homebridge setup code shown at startup.');
  }
}
