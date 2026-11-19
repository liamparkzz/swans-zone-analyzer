"""Find untouched SWANS zones from MQSim-format traces without running MQSim."""
import argparse
import csv
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Geometry:
    ssd_count: int
    stripe_lba: int
    zone_lba: int
    backend_sectors_per_ssd: int
    visible_sectors_per_ssd: int
    user_zones_per_ssd: int
    reserve: bool
    host_interface: str

    @property
    def capacity(self):
        return self.visible_sectors_per_ssd * self.ssd_count

    @property
    def user_zones(self):
        return self.user_zones_per_ssd * self.ssd_count

    @property
    def total_zones(self):
        return self.user_zones + (self.ssd_count if self.reserve else 0)


def settings(element, strict_alias=False):
    result = {}
    seen_tags = set()
    for node in element:
        if len(node):
            continue
        key = 'op_zone_migration' if node.tag == 'SWANS_Reserve_Zone_Enabled' else node.tag
        value = (node.text or '').strip()
        if node.tag in seen_tags or (key in result and (strict_alias or result[key] != value)):
            raise ValueError(f'duplicate/conflicting setting: {key}')
        seen_tags.add(node.tag)
        result[key] = value
    return result


def boolean(value):
    if value not in ('true', 'false'):
        raise ValueError(f'expected true/false, got {value!r}')
    return value == 'true'


def load_geometry(ssd_path, swans_path=None, workload_path=None):
    device = ET.parse(ssd_path).find('.//Device_Parameter_Set')
    if device is None:
        raise ValueError('SSD XML is missing Device_Parameter_Set')
    config = settings(device)
    flash = device.find('Flash_Parameter_Set')
    if flash is None:
        raise ValueError('SSD XML is missing Flash_Parameter_Set')
    config.update(settings(flash))
    if swans_path:
        policy = ET.parse(swans_path).getroot()
        if policy.tag != 'SWANS_Parameter_Set':
            raise ValueError('expected SWANS_Parameter_Set root')
        config.update(settings(policy, strict_alias=True))
    dims = ['Flash_Channel_Count', 'Chip_No_Per_Channel', 'Die_No_Per_Chip', 'Plane_No_Per_Die']
    counts = [int(config[key]) for key in dims]
    percent = 100.0
    if workload_path:
        scenarios = list(ET.parse(workload_path).getroot())
        if len(scenarios) != 1 or len(scenarios[0]) != 1 or scenarios[0][0].tag != 'IO_Flow_Parameter_Set_Trace_Based':
            raise ValueError('workload must contain exactly one scenario and one trace flow')
        flow = scenarios[0][0]
        for i, key in enumerate(['Channel_IDs', 'Chip_IDs', 'Die_IDs', 'Plane_IDs']):
            text = flow.findtext(key)
            if text is None:
                raise ValueError(f'workload is missing {key}')
            ids = [int(v.strip()) for v in text.split(',')]
            if len(set(ids)) != len(ids) or any(v < 0 or v >= counts[i] for v in ids):
                raise ValueError(f'invalid workload {key}')
            counts[i] = len(ids)
        relay = int(flow.findtext('Relay_Count', '1'))
        # Repeating a trace never adds new touched addresses; EOL timing is not modeled.
        percent = float(flow.findtext('Percentage_To_Be_Executed', '100')) if relay == 1 else 100.0
        if not math.isfinite(percent) or percent <= 0:
            raise ValueError('Percentage_To_Be_Executed must be in (0,100]')
        percent = min(100.0, percent)
    page_bytes = int(config['Page_Capacity'])
    blocks, pages = int(config['Block_No_Per_Plane']), int(config['Page_No_Per_Block'])
    op = float(config['Overprovisioning_Ratio'])
    ssd_count = max(1, int(config.get('SSD_Count', '1')))
    stripe = int(config.get('Stripe_Unit_LBA', '8'))
    zone = int(config.get('SWANS_Zone_Size_LBA', '0'))
    if zone == 0:
        zone = stripe * max(1, int(config.get('Zone_Stripe_Multiplier', '128')))
    if min(counts + [blocks, pages, page_bytes, stripe, zone]) <= 0 or page_bytes % 512 or not 0 <= op < 1:
        raise ValueError('invalid flash/stripe/zone geometry or OP ratio')
    if zone % stripe:
        raise ValueError('zone size must be a multiple of stripe size')
    # MQSim truncates the logical sector count separately for each allocated plane.
    backend = math.prod(counts) * int(blocks * pages * (page_bytes // 512) * (1.0 - op))
    reserve = boolean(config.get('op_zone_migration', 'false'))
    if reserve and (zone % (page_bytes // 512) or backend // zone < 2):
        raise ValueError('reserve needs two full zones per SSD and page-aligned zones')
    visible = (backend // zone - 1) * zone if reserve else backend
    if visible <= 1:
        raise ValueError('exported capacity is too small')
    geometry = Geometry(ssd_count, stripe, zone, backend, visible,
                        (visible + zone - 1) // zone, reserve, config.get('HostInterface_Type', 'NVME'))
    if geometry.host_interface not in ('NVME', 'SATA'):
        raise ValueError('supported HostInterface_Type: NVME or SATA')
    return geometry, percent


def trace_records(path):
    previous = -1
    with path.open(encoding='utf-8-sig') as source:
        for line_no, line in enumerate(source, 1):
            parts = line.split()
            if not parts:
                continue
            try:
                if len(parts) != 5:
                    raise ValueError('expected time device lba sectors op (0=write,1=read)')
                time, device, lba, sectors, op = map(int, parts)
                if min(time, device, lba) < 0 or not 0 < sectors <= 65535 or op not in (0, 1):
                    raise ValueError('invalid trace value; sectors must be 1..65535')
                if time < previous:
                    raise ValueError('timestamps must not decrease')
                previous = time
            except ValueError as exc:
                raise ValueError(f'{path}:{line_no}: {exc}') from exc
            yield lba, sectors, op


def mark_request(used, geometry, lba, sectors, allow_folding=True):
    if not allow_folding and lba + sectors > geometry.capacity:
        raise ValueError(f'address folding forbidden: request [{lba},{lba+sectors-1}] exceeds {geometry.capacity} sectors')
    # IO_Flow_Trace_Based uses end-start (capacity-1), not capacity, for oversized LBA.
    if lba >= geometry.capacity:
        lba %= geometry.capacity - 1
    if geometry.host_interface == 'NVME':
        # Preserve the existing NVMe decoder's shift by 31 when reconstructing the LBA.
        lba = ((lba >> 32) << 31) | (lba & 0xffffffff)
    end = lba + sectors - 1
    if end >= geometry.capacity:
        raise ValueError(f'request [{lba},{end}] crosses exported capacity {geometry.capacity}')
    added = 0
    if geometry.stripe_lba == geometry.zone_lba:
        for zone in range(lba // geometry.zone_lba, end // geometry.zone_lba + 1):
            if not used[zone]:
                used[zone] = 1
                added += 1
        return added
    stripe, disks = geometry.stripe_lba, geometry.ssd_count
    first, last = lba // stripe, end // stripe
    for disk in range(disks):
        first_disk_stripe = first + (disk - first) % disks
        last_disk_stripe = last - (last - disk) % disks
        if first_disk_stripe > last_disk_stripe:
            continue
        local_start = first_disk_stripe // disks * stripe + (lba % stripe if first_disk_stripe == first else 0)
        local_end = last_disk_stripe // disks * stripe + (end % stripe if last_disk_stripe == last else stripe - 1)
        begin = local_start // geometry.zone_lba
        finish = local_end // geometry.zone_lba
        for zone in range(begin * disks + disk, finish * disks + disk + 1, disks):
            if not used[zone]:
                used[zone] = 1
                added += 1
    return added


def analyze(geometry, paths, access='read-write', percent=100.0, allow_folding=True):
    used = bytearray(geometry.total_zones)
    stats = {'read_requests': 0, 'write_requests': 0, 'considered_requests': 0,
             'zone_state_updates': 0, 'already_used_zone_requests': 0}
    for path in paths:
        limit = int(sum(1 for _ in trace_records(path)) * (percent / 100)) if percent < 100 else None
        for index, (lba, sectors, op) in enumerate(trace_records(path)):
            if limit is not None and index >= limit:
                break
            stats['write_requests' if op == 0 else 'read_requests'] += 1
            if access == 'writes' and op == 1:
                continue
            added = mark_request(used, geometry, lba, sectors, allow_folding)
            stats['zone_state_updates'] += added
            stats['already_used_zone_requests'] += added == 0
            stats['considered_requests'] += 1
    return used, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssd', required=True, type=Path)
    parser.add_argument('--swans', type=Path)
    parser.add_argument('--workload', type=Path, help='optional single trace-flow resource/percentage settings')
    parser.add_argument('--trace', required=True, nargs='+', type=Path, help='files in the same logical address space')
    parser.add_argument('--access', choices=['read-write', 'writes'], default='read-write')
    parser.add_argument('--out', required=True, type=Path, help='new output folder')
    parser.add_argument('--no-folding', action='store_true', help='reject any request outside the original user address range')
    args = parser.parse_args()
    try:
        geometry, percent = load_geometry(args.ssd, args.swans, args.workload)
        used, stats = analyze(geometry, args.trace, args.access, percent, allow_folding=not args.no_folding)
        summary = {'geometry': asdict(geometry), 'ssd_config': str(args.ssd.resolve()),
                   'swans_config': str(args.swans.resolve()) if args.swans else None,
                   'workload': str(args.workload.resolve()) if args.workload else None,
                   'traces': [str(p.resolve()) for p in args.trace], 'access': args.access,
                   'percentage': percent, 'address_folding': 'FORBIDDEN' if args.no_folding else 'MQSIM_RUNTIME_RULE',
                   **stats, 'user_zones': geometry.user_zones,
                   'used_user_zones': used.count(1), 'empty_user_zones': geometry.user_zones - used.count(1),
                   'reserve_zones': geometry.total_zones - geometry.user_zones,
                   'scope': 'TRACE_LOGICAL_ZONE_FOOTPRINT_NO_MIGRATION_NO_EOL',
                   'initial_data': 'NOT_MODELED_TRACE_ADDRESSES_ONLY', 'per_ssd': []}
        for disk in range(geometry.ssd_count):
            occupied = used[disk:geometry.user_zones:geometry.ssd_count].count(1)
            summary['per_ssd'].append({'ssd_id': disk, 'user_zones': geometry.user_zones_per_ssd,
                                       'used_user_zones': occupied,
                                       'empty_user_zones': geometry.user_zones_per_ssd - occupied})
        args.out.mkdir(parents=True, exist_ok=False)
        with (args.out / 'zones.csv').open('w', newline='', encoding='utf-8-sig') as output:
            writer = csv.writer(output)
            writer.writerow(['zone_id', 'initial_ssd_id', 'local_zone_id', 'kind', 'empty'])
            for zone_id, present in enumerate(used):
                writer.writerow([zone_id, zone_id % geometry.ssd_count, zone_id // geometry.ssd_count,
                                 'user' if zone_id < geometry.user_zones else 'reserve', not bool(present)])
        with (args.out / 'empty_zones.txt').open('w', encoding='utf-8') as output:
            for zone_id in range(geometry.user_zones):
                if not used[zone_id]:
                    output.write(f'{zone_id}\n')
        (args.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        parser.exit(1, f'Error: {exc}\n')
    print(f"User zones: total={geometry.user_zones}, used={summary['used_user_zones']}, empty={summary['empty_user_zones']}")
    print(f"Reserve zones (separate): {summary['reserve_zones']}; zone size={geometry.zone_lba} LBA")
    print('Scope: trace logical footprint; no SSD I/O, migration, or EOL simulation')
    print(f'Results: {args.out.resolve()}')


if __name__ == '__main__':
    main()
