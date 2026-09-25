"""Collect Raspberry Pi health samples over SSH without writing to the Pi."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


REMOTE_SAMPLE = r'''
import json
import os
import re
import subprocess
import time

def run(*args, timeout=5):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def meminfo():
    values = {}
    with open('/proc/meminfo', encoding='ascii') as source:
        for line in source:
            key, value = line.split(':', 1)
            values[key] = int(value.strip().split()[0]) * 1024
    return values

def disk(path):
    stat = os.statvfs(path)
    return {'total_bytes': stat.f_blocks * stat.f_frsize,
            'free_bytes': stat.f_bavail * stat.f_frsize}

memory = meminfo()
try:
    with open('/sys/class/thermal/thermal_zone0/temp', encoding='ascii') as source:
        cpu_temp_c = int(source.read().strip()) / 1000
except (OSError, ValueError):
    cpu_temp_c = None
throttled = run('vcgencmd', 'get_throttled')
kernel = run('dmesg', timeout=5)
containers = run('docker', 'ps', '-a', '--format', '{{.Names}}|{{.Status}}', timeout=6)
stats = run('docker', 'stats', '--no-stream',
            '--format', '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.BlockIO}}', timeout=8)
print(json.dumps({
    'remote_time_unix': time.time(),
    'cpu_temp_c': cpu_temp_c,
    'throttled': throttled,
    'mem_available_bytes': memory.get('MemAvailable'),
    'swap_free_bytes': memory.get('SwapFree'),
    'root_disk': disk('/'),
    'nvme_disk': disk('/mnt/nvme'),
    'loadavg': os.getloadavg(),
    'nvme_write_timeout_count': len(re.findall(r'nvme .*timeout, aborting req_op:WRITE', kernel or '')),
    'containers': containers.splitlines() if containers is not None else None,
    'container_stats': stats.splitlines() if stats is not None else None,
}, separators=(',', ':')))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='raspberrypi5@192.168.15.62')
    parser.add_argument('--known-hosts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--interval-seconds', type=float, default=10)
    parser.add_argument('--duration-seconds', type=float, default=24 * 3600)
    args = parser.parse_args()
    if args.interval_seconds <= 0 or args.duration_seconds <= 0:
        parser.error('interval and duration must be positive')
    if not args.known_hosts.is_file():
        parser.error('known-hosts file is missing')

    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        'ssh', '-F', '/dev/null', '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=yes',
        '-o', f'UserKnownHostsFile={args.known_hosts}', args.host,
        'python3 -c ' + shlex.quote(REMOTE_SAMPLE),
    ]
    deadline = time.monotonic() + args.duration_seconds
    with args.output.open('a', encoding='utf-8') as output:
        while time.monotonic() < deadline:
            started = time.monotonic()
            record = {'sampled_at_utc': datetime.now(timezone.utc).isoformat()}
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=25)
                if result.returncode == 0:
                    record.update(json.loads(result.stdout))
                else:
                    record['error'] = result.stderr.strip()[:400]
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                record['error'] = str(exc)[:400]
            output.write(json.dumps(record, separators=(',', ':')) + '\n')
            output.flush()
            time.sleep(max(0, args.interval_seconds - (time.monotonic() - started)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
