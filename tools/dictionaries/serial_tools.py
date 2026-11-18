"""Small helpers shared by the diagnostic serial clients."""

class LineReader:
    def __init__(self):
        self.pending = bytearray()

    def read(self, port):
        self.pending.extend(port.readline())
        if not self.pending.endswith(b'\n'):
            return None
        line = bytes(self.pending).rstrip(b'\r\n')
        self.pending.clear()
        return line


def find_port():
    try:
        from serial.tools import list_ports
    except ImportError:
        raise RuntimeError('Install diagnostics dependencies with: python3 -m pip install pyserial') from None
    ports = list(list_ports.comports())
    matches = [p for p in ports if 'Steno Squid' in
               (getattr(p, 'product', '') or getattr(p, 'description', '') or '')]
    if len(matches) == 1:
        return matches[0].device
    available = ', '.join(p.device for p in (matches or ports)) or 'none'
    raise RuntimeError(f'Connect one Left-Diagnostic keyboard or supply its serial port. Available: {available}.')
