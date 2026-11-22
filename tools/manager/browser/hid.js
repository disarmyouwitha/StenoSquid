// The vendor console descriptor is shared by USB and Bluetooth firmware.
export const filter = {usagePage: 0xff31, usage: 0x74};
export function consoleCollection(device) {
  return device.collections.find((row) => row.usagePage === filter.usagePage && row.usage === filter.usage);
}

export class HidConnection {
  constructor(hid, disconnected) {
    this.hid = hid;
    this.disconnected = disconnected;
    this.packets = [];
    this.pending = null;
    this.device = null;
    this.input = ({reportId, data}) => {
      if (reportId !== this.inputId) return;
      const packet = Array.from(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
      if (this.pending) this.finish(packet);
      else this.packets.push(packet);
    };
    hid.addEventListener("disconnect", ({device}) => {
      if (device !== this.device) return;
      this.finish([], new Error("Keyboard disconnected. Select Connect to try again."));
      this.packets = [];
      this.needsReconnect = true;
      this.disconnected();
    });
  }
  finish(packet, error) {
    if (!this.pending) return;
    const {resolve, reject, timer} = this.pending;
    this.pending = null;
    clearTimeout(timer);
    if (error) reject(error);
    else resolve(packet);
  }
  async choose(force = false) {
    // Checking existing grants does not need a chooser. If there are none,
    // this short API call preserves the Connect click's user activation.
    if (!force) {
      const granted = (await this.hid.getDevices()).filter(consoleCollection);
      if (granted.length) return {devices: granted, explicit: false};
    }
    const selected = await this.hid.requestDevice({filters: [filter]});
    if (selected.length !== 1) throw new Error("Choose one Steno Squid left half to connect.");
    const granted = (await this.hid.getDevices()).filter(consoleCollection);
    return {devices: [selected[0], ...granted.filter(device => device !== selected[0])], explicit: true};
  }
  async select(devices) {
    if (devices.length !== 1) throw new Error("Choose one Steno Squid left half to connect.");
    await this.close();
    const device = devices[0];
    const collection = consoleCollection(device);
    const input = collection?.inputReports;
    const output = collection?.outputReports;
    if (input?.length !== 1 || output?.length !== 1) throw new Error("The selected device does not expose the Steno Squid console.");
    this.device = device;
    this.needsReconnect = false;
    this.inputId = input[0].reportId;
    this.outputId = output[0].reportId;
    device.addEventListener("inputreport", this.input);
    await this.open();
  }
  async open() {
    if (!this.device) throw new Error("Select Connect and choose the Steno Squid left half.");
    if (!this.device.opened || this.needsReconnect) {
      try {
        if (this.needsReconnect) throw new Error("Keyboard rebooted; reconnecting…");
        await this.device.open();
      } catch (error) {
        // A reboot can replace the browser's HIDDevice object. Reconnect only
        // when exactly one authorized console exists; Python checks its serial
        // again before issuing any operation.
        const devices = (await this.hid.getDevices()).filter(device => consoleCollection(device) &&
          device.productName === this.device.productName && device.vendorId === this.device.vendorId &&
          device.productId === this.device.productId);
        if (devices.length !== 1) throw error;
        const candidate = devices[0];
        const collection = consoleCollection(candidate);
        this.device.removeEventListener("inputreport", this.input);
        this.device = candidate;
        this.inputId = collection.inputReports[0].reportId;
        this.outputId = collection.outputReports[0].reportId;
        candidate.addEventListener("inputreport", this.input);
        await candidate.open();
        this.needsReconnect = false;
      }
    }
  }
  async close() {
    this.finish([], new Error("Keyboard connection closed."));
    this.packets = [];
    if (this.device) {
      this.device.removeEventListener("inputreport", this.input);
      if (this.device.opened) await this.device.close();
      this.device = null;
    }
  }
  async write(data) {
    await this.open();
    await this.device.sendReport(this.outputId, new Uint8Array(data));
  }
  read(timeout) {
    if (this.packets.length) return Promise.resolve(this.packets.shift());
    if (this.pending) return Promise.reject(new Error("A HID read is already pending."));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.finish([]), timeout);
      this.pending = {resolve, reject, timer};
    });
  }
}
