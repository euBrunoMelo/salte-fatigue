"""Export CM5 bench evidence over SSH without creating files on the Pi.

Run this on a computer that can already authenticate to the Pi. The collector
only reads remote state. It never invokes a probe, capture, reboot, or disk
stress operation. Review the resulting files before sharing them: system logs
and configuration may contain private network or device information.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone


REMOTE_COLLECTOR = r'''
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

MAX_COMMAND_BYTES = 12 * 1024 * 1024
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')

def boot_id():
    try:
        return BOOT_ID.read_text(encoding='ascii').strip()
    except OSError:
        return 'unknown'

def emit(name, data, *, status='ok', returncode=None, error='', truncated=False,
         boot_start=None):
    boot_end = boot_id()
    print(json.dumps({
        'name': name,
        'boot_id_start': boot_start or boot_end,
        'boot_id_end': boot_end,
        'status': status,
        'returncode': returncode,
        'error': error,
        'truncated': truncated,
        'data_b64': base64.b64encode(data).decode('ascii'),
    }, separators=(',', ':')), flush=True)

def run(name, argv, timeout=15, limit=MAX_COMMAND_BYTES):
    started = boot_id()
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout,
                                check=False)
        data = result.stdout
        truncated = len(data) > limit
        emit(name, data[:limit], status='ok' if result.returncode == 0 else 'error',
             returncode=result.returncode,
             error=result.stderr.decode('utf-8', 'replace')[:2000],
             truncated=truncated, boot_start=started)
    except subprocess.TimeoutExpired as exc:
        emit(name, (exc.stdout or b'')[:limit], status='timeout',
             error='command exceeded %s seconds' % timeout,
             truncated=len(exc.stdout or b'') > limit, boot_start=started)
    except OSError as exc:
        emit(name, b'', status='error', error=str(exc), boot_start=started)

def read(name, path, limit=1024 * 1024):
    started = boot_id()
    try:
        with open(path, 'rb') as source:
            data = source.read(limit + 1)
        emit(name, data[:limit], truncated=len(data) > limit, boot_start=started)
    except OSError as exc:
        emit(name, b'', status='error', error=str(exc), boot_start=started)

def snapshot():
    result = {}
    for device in ('0-001a', '0-0040', '10-001a', '10-0040'):
        path = Path('/sys/bus/i2c/devices') / device
        entry = {'present': path.exists()}
        if path.exists():
            for field in ('name', 'modalias', 'power/runtime_status'):
                try:
                    entry[field] = (path / field).read_text(errors='replace').strip()
                except OSError as exc:
                    entry[field] = {'error': str(exc)}
            for field in ('driver', 'of_node'):
                try:
                    entry[field] = os.path.realpath(path / field) if (path / field).exists() else None
                except OSError as exc:
                    entry[field] = {'error': str(exc)}
        result[device] = entry
    regulators = {}
    base = Path('/sys/class/regulator')
    try:
        paths = sorted(base.iterdir())
    except OSError:
        paths = []
    for path in paths:
        values = {}
        for field in ('name', 'state', 'num_users', 'microvolts'):
            try:
                values[field] = (path / field).read_text(errors='replace').strip()
            except OSError:
                pass
        if values:
            regulators[path.name] = values
    result['regulators'] = regulators
    emit('sysfs/camera_and_regulators.json',
         (json.dumps(result, indent=2, sort_keys=True) + '\n').encode())

def inventory():
    root = Path.home()
    pattern = re.compile(r'(bench|bancada|ensaio|test|teste|valid|diagnos|hardware|camera|cam[01]|nvme|health|eviden|rp2040|bridge|manifest)', re.I)
    evidence_dir = re.compile(r'(bench|bancada|ensaio|valid|diagnos|eviden|logs?)', re.I)
    excluded = {'.git', '.cache', '.local', '.ssh', '.gnupg', '.venv',
                'venv', 'node_modules', '__pycache__'}
    items = []
    inspected = 0
    for current, dirs, files in os.walk(root):
        depth = len(Path(current).relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if d not in excluded) if depth < 5 else []
        for filename in sorted(files):
            inspected += 1
            if inspected > 30000:
                break
            path = Path(current) / filename
            relative = path.relative_to(root)
            if not (pattern.search(filename) or
                    (evidence_dir.search(str(relative.parent)) and
                     path.suffix.lower() in {'.txt', '.md', '.log', '.json', '.jsonl', '.csv'})):
                continue
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
                if not path.is_file():
                    continue
                items.append({'path': str(path), 'bytes': stat.st_size,
                              'mtime_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                                         time.gmtime(stat.st_mtime))})
            except OSError:
                continue
            if len(items) >= 1000:
                break
        if inspected > 30000 or len(items) >= 1000:
            break
    emit('inventory/home_candidates.json',
         (json.dumps({'home': str(root), 'files_inspected': inspected,
                      'inventory_limited': inspected > 30000 or len(items) >= 1000,
                      'candidates': items}, indent=2) + '\n').encode())

def collect_run_logs():
    root = Path.home() / 'salte-fatigue-staging-20260925/logs/runs'
    if not root.is_dir() or root.is_symlink():
        emit('runtime/run_logs_index.json',
             b'{"status":"runs directory unavailable"}\n', status='error')
        return
    device = root.stat().st_dev
    allowed = {'run-start.json', 'frames.jsonl', 'assessments.jsonl', 'manifest.json'}
    selected = []
    skipped = []
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        depth = len(Path(current).relative_to(root).parts)
        next_dirs = []
        if depth < 10:
            for dirname in sorted(dirs):
                child = Path(current) / dirname
                try:
                    if not child.is_symlink() and child.stat().st_dev == device:
                        next_dirs.append(dirname)
                except OSError:
                    skipped.append(str(child.relative_to(root)))
        dirs[:] = next_dirs
        for filename in sorted(files):
            if filename not in allowed:
                continue
            path = Path(current) / filename
            relative = path.relative_to(root)
            try:
                stat = path.stat()
            except OSError:
                skipped.append(str(relative))
                continue
            if path.is_symlink() or stat.st_dev != device or not path.is_file():
                skipped.append(str(relative))
                continue
            if (not re.fullmatch(r'[A-Za-z0-9_./-]+', str(relative)) or
                    stat.st_size > 12 * 1024 * 1024 or
                    total + stat.st_size > 64 * 1024 * 1024 or
                    len(selected) >= 300):
                skipped.append(str(relative))
                continue
            read('runtime/run_logs/' + str(relative), path, limit=12 * 1024 * 1024)
            total += stat.st_size
            selected.append({'path': str(relative), 'bytes': stat.st_size})
    emit('runtime/run_logs_index.json',
         (json.dumps({'selected': selected, 'skipped': skipped,
                      'total_bytes': total}, indent=2) + '\n').encode())

emit('meta/boot_id_start.txt', (boot_id() + '\n').encode())
run('meta/hostname.txt', ['hostname'])
run('meta/date.txt', ['date', '-Is'])
run('meta/uname.txt', ['uname', '-a'])
run('meta/uptime_start.txt', ['uptime', '-s'])
run('meta/groups.txt', ['id', '-nG'])
run('meta/mounts.txt', ['findmnt', '-no', 'SOURCE,TARGET,FSTYPE'])
run('meta/boot_history.txt', ['journalctl', '--list-boots', '--no-pager'])
read('boot/proc_cmdline.txt', '/proc/cmdline')
read('boot/cmdline.txt', '/boot/firmware/cmdline.txt')
read('boot/config.txt', '/boot/firmware/config.txt')
read('boot/kernel_config.txt', '/boot/config-' + os.uname().release)
config = Path('/boot/firmware/config.txt')
try:
    includes = re.findall(r'^\s*include\s+([^\s#]+)', config.read_text(errors='replace'), re.M)
except OSError:
    includes = []
for index, name in enumerate(includes[:20]):
    candidate = (config.parent / name).resolve()
    if candidate.parent == config.parent:
        read('boot/include_%02d.txt' % index, candidate)
run('boot/firmware_config.txt', ['vcgencmd', 'get_config', 'int'])
run('boot/device_tree.dts', ['dtc', '-I', 'fs', '-O', 'dts', '/proc/device-tree'], timeout=20)
run('logs/kernel_current.txt', ['journalctl', '-k', '-b', '--no-pager', '-o', 'short-iso-precise'], timeout=35)
run('logs/kernel_retained.txt', ['journalctl', '-k', '--no-pager', '-o', 'short-iso-precise'], timeout=45)
run('logs/kernel_hardware_root.txt', ['sudo', '-n', 'journalctl', '-k',
    '--no-pager', '-o', 'short-iso-precise', '-g',
    'rp2040|imx500|nvme|pcie|regulator|cfe|[Uu]nder-voltage|throttl|i2c'],
    timeout=35)
run('logs/services.txt', ['systemctl', 'list-units', '--all', '--no-pager', '--plain'], timeout=12)
snapshot()
run('camera/trace_events.txt', ['ls', '-1', '/sys/kernel/tracing/events/i2c',
                                '/sys/kernel/tracing/events/regulator',
                                '/sys/kernel/tracing/events/gpio',
                                '/sys/kernel/tracing/events/rpm'])
run('camera/regulator_summary.txt', ['sudo', '-n', 'cat', '/sys/kernel/debug/regulator/regulator_summary'])
run('camera/pin_34.txt', ['pinctrl', 'get', '34'])
read('camera/bridge_srcversion.txt', '/sys/module/spi_rp2040_gpio_bridge/srcversion')
read('camera/bridge_build_id.note', '/sys/module/spi_rp2040_gpio_bridge/notes/.note.gnu.build-id')
run('camera/bridge_modinfo.txt', ['/usr/sbin/modinfo', 'spi-rp2040-gpio-bridge'])
run('camera/packages.txt', ['dpkg-query', '-W', '-f=${Package} ${Version}\n',
                            'linux-image-*', 'imx500-all', 'rpicam-apps',
                            'python3-picamera2', 'libcamera*'])
read('health/aspm_policy.txt', '/sys/module/pcie_aspm/parameters/policy')
read('health/nvme_state.txt', '/sys/class/nvme/nvme0/state')
read('health/nvme_model.txt', '/sys/class/nvme/nvme0/model')
run('health/throttled.txt', ['vcgencmd', 'get_throttled'])
run('health/temperature.txt', ['vcgencmd', 'measure_temp'])
run('runtime/isolated_docker_ps.txt', ['sudo', '-n', 'docker', '-H',
    'unix:///run/salte-fatigue-docker/docker.sock', 'ps', '-a', '--no-trunc'], timeout=10)
run('runtime/fatigue_logs_tail.txt', ['sudo', '-n', 'docker', '-H',
    'unix:///run/salte-fatigue-docker/docker.sock', 'logs', '--tail', '250',
    'salte-fatigue'], timeout=12)
inventory()
collect_run_logs()
emit('meta/boot_id_end.txt', (boot_id() + '\n').encode())
emit('meta/collection_complete.txt', b'complete\n')
'''


SAFE_NAME = re.compile(r"^[A-Za-z0-9_./-]+$")


def destination_name(name: str) -> Path:
    path = Path(name)
    if not SAFE_NAME.fullmatch(name) or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe remote record name: {name!r}")
    return path


def write_manifest(directory: Path) -> None:
    entries = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "manifest.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(f"{digest}  {path.relative_to(directory)}")
    (directory / "manifest.sha256").write_text("\n".join(entries) + "\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="raspberrypi5@100.120.192.2")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.host):
        parser.error("invalid SSH host")
    if args.output_dir.exists():
        if not args.output_dir.is_dir() or any(args.output_dir.iterdir()):
            parser.error("output directory must be an empty directory")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    command = ["ssh", "-T", "-o", "ConnectTimeout=10", args.host,
               "python3", "-B", "-"]
    try:
        result = subprocess.run(command, input=REMOTE_COLLECTOR.encode(),
                                capture_output=True, timeout=360, check=False)
        stream = result.stdout
        ssh_status = "ok" if result.returncode == 0 else "error"
        ssh_error = result.stderr.decode(errors="replace")[:4000]
    except subprocess.TimeoutExpired as exc:
        stream = exc.stdout or b""
        ssh_status = "timeout"
        ssh_error = "SSH collection exceeded 360 seconds"
    except OSError as exc:
        print(f"SSH collection failed: {exc}", file=sys.stderr)
        return 1

    index = []
    boot_ids = set()
    try:
        lines = stream.splitlines()
        for number, line in enumerate(lines):
            if ssh_status != "ok" and number == len(lines) - 1:
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    break  # Incomplete final record after SSH failed.
            record = json.loads(line)
            name = destination_name(record["name"])
            boot_start = record["boot_id_start"]
            boot_end = record["boot_id_end"]
            for boot_id in (boot_start, boot_end):
                if not re.fullmatch(r"[0-9a-f-]{36}|unknown", boot_id):
                    raise ValueError(f"invalid boot ID: {boot_id!r}")
                boot_ids.add(boot_id)
            group = boot_start if boot_start == boot_end else "mixed_boot"
            path = args.output_dir / group / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(record.pop("data_b64"), validate=True))
            record["file"] = str(path.relative_to(args.output_dir))
            index.append(record)
    except (ValueError, KeyError, OSError) as exc:
        print(f"invalid collection stream: {exc}", file=sys.stderr)
        return 1
    if not index:
        print(f"collection returned no records: {ssh_error}", file=sys.stderr)
        return 1
    if not any(item["name"] == "meta/collection_complete.txt" for item in index):
        ssh_status = "incomplete"
        ssh_error = "remote collector ended before its completion marker"

    summary = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": args.host,
        "boot_ids": sorted(boot_ids),
        "boot_changed": len(boot_ids) > 1,
        "ssh_status": ssh_status,
        "ssh_error": ssh_error,
        "records": index,
    }
    (args.output_dir / "collection-index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_manifest(args.output_dir)
    errors = sum(item["status"] != "ok" or item["truncated"] for item in index)
    print(f"Evidence: {args.output_dir}")
    print(f"Records: {len(index)}; incomplete records: {errors}; boot IDs: {', '.join(sorted(boot_ids))}")
    print(f"Verify: cd {shlex.quote(str(args.output_dir))} && sha256sum -c manifest.sha256")
    if len(boot_ids) > 1:
        print("Boot changed during collection; inspect per-boot directories before analysis.",
              file=sys.stderr)
    if ssh_status != "ok":
        print(f"SSH collection incomplete: {ssh_error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
