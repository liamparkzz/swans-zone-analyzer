"""Scan original MSRC CSVs and solve the minimum-request whole-file zone cover."""
import argparse
import copy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import time

from zone_check import Geometry, load_geometry


def mark_new_zones(mask, first, last):
    if first == last:
        if mask[first]:
            return 0
        mask[first] = 1
        return 1
    added = 0
    for zone in range(first, last + 1):
        if not mask[zone]:
            mask[zone] = 1
            added += 1
    return added


def scan(path, geometry, no_folding=False):
    if geometry.zone_lba != geometry.stripe_lba:
        raise ValueError('this batch scanner requires zone size == stripe unit')
    masks = {name: bytearray(geometry.user_zones) for name in ['read_write', 'writes', 'unfolded']}
    stats = {name: 0 for name in ['requests', 'reads', 'writes', 'read_bytes', 'write_bytes',
                                  'runtime_remapped_requests', 'crosses_capacity', 'oversized_requests',
                                  'out_of_order_timestamps', 'out_of_range_requests',
                                  'zone_state_updates', 'zone_skip_requests', 'zone_new_requests']}
    sha = hashlib.sha256()
    devices = set()
    previous = -1
    max_end = 0
    with path.open('rb') as source:
        for row_no, line in enumerate(source, 1):
            sha.update(line)
            fields = line.rstrip().split(b',')
            if len(fields) != 7:
                raise ValueError(f'{path}:{row_no}: expected 7 original MSRC fields')
            stamp, host, disk, operation, offset, size, response = fields
            stamp, offset, size = int(stamp), int(offset), int(size)
            if operation not in (b'Read', b'Write') or offset < 0 or offset % 512 or size <= 0 or size % 512:
                raise ValueError(f'{path}:{row_no}: invalid original request')
            devices.add((host.decode('ascii'), disk.decode('ascii')))
            stats['out_of_order_timestamps'] += stamp < previous
            previous = stamp
            stats['requests'] += 1
            write = operation == b'Write'
            stats['writes' if write else 'reads'] += 1
            stats['write_bytes' if write else 'read_bytes'] += size
            lba, sectors = offset // 512, size // 512
            max_end = max(max_end, offset + size)
            if sectors > 65535:
                stats['oversized_requests'] += 1
                continue
            if lba + sectors > geometry.capacity:
                stats['out_of_range_requests'] += 1
                if no_folding:
                    continue
            if lba + sectors <= geometry.capacity:
                first, last = lba // geometry.zone_lba, (lba + sectors - 1) // geometry.zone_lba
                mark_new_zones(masks['unfolded'], first, last)
            if lba >= geometry.capacity:
                stats['runtime_remapped_requests'] += 1
                lba %= geometry.capacity - 1
            if geometry.host_interface == 'NVME':
                lba = ((lba >> 32) << 31) | (lba & 0xffffffff)
            if lba + sectors > geometry.capacity:
                stats['crosses_capacity'] += 1
                continue
            first, last = lba // geometry.zone_lba, (lba + sectors - 1) // geometry.zone_lba
            added = mark_new_zones(masks['read_write'], first, last)
            stats['zone_state_updates'] += added
            stats['zone_new_requests' if added else 'zone_skip_requests'] += 1
            if write:
                mark_new_zones(masks['writes'], first, last)
    encoded = {}
    for name, present in masks.items():
        value = sum(1 << zone for zone, flag in enumerate(present) if flag)
        encoded[name] = {'used_zones': value.bit_count(), 'mask_hex': hex(value)}
    return {'path': str(path.resolve()), 'name': path.name, 'bytes': path.stat().st_size,
            'mtime_ns': path.stat().st_mtime_ns, 'sha256': sha.hexdigest(), **stats,
            'devices': sorted(devices), 'max_original_end_bytes': max_end,
            'valid_for_native_replay': not (stats['crosses_capacity'] or stats['oversized_requests'] or stats['out_of_order_timestamps']),
            'coverage': encoded}


def scan_native(path, geometry, binary, no_folding=False):
    if geometry.zone_lba != geometry.stripe_lba:
        raise ValueError('this batch scanner requires zone size == stripe unit')
    run = subprocess.run([str(binary), str(path), str(geometry.capacity), str(geometry.zone_lba),
                          '1' if geometry.host_interface == 'NVME' else '0', '0' if no_folding else '1'], capture_output=True, text=True)
    if run.returncode:
        raise ValueError(f'{path}: native parser failed: {run.stderr}')
    record = json.loads(run.stdout)
    record.update(path=str(path.resolve()), name=path.name, bytes=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns)
    return record


def no_folding_result(records, geometry):
    # Entire originals must fit. Do not drop outside requests to make a file eligible.
    eligible = [r for r in records if r['max_original_end_bytes'] <= geometry.capacity * 512
                and not r['oversized_requests'] and not r['out_of_order_timestamps']]
    result = solve(eligible, geometry.user_zones, mode='unfolded', strict=True)
    union = 0
    for row in eligible:
        union |= int(row['coverage']['unfolded']['mask_hex'], 16)
    result.update(address_folding='FORBIDDEN', whole_original_files=True,
                  user_zone_count=geometry.user_zones,
                  reserve_zone_count_excluded=geometry.total_zones-geometry.user_zones,
                  excluded_files=[{'name': r['name'], 'max_original_end_bytes': r['max_original_end_bytes']}
                                  for r in records if r not in eligible],
                  eligible_files=[{'name': r['name'], 'path': r['path'], 'requests': r['requests']}
                                  for r in eligible], per_ssd=[])
    for disk in range(geometry.ssd_count):
        used = sum(bool(union >> z & 1) for z in range(disk, geometry.user_zones, geometry.ssd_count))
        result['per_ssd'].append({'ssd_id': disk, 'used_user_zones': used,
                                 'empty_user_zones': geometry.user_zones_per_ssd-used})
    return result


def coarsen_no_folding_records(records, old_geometry, new_geometry):
    """Merge adjacent raw-address zones without rereading the original requests."""
    if (old_geometry.zone_lba != old_geometry.stripe_lba
            or new_geometry.zone_lba != new_geometry.stripe_lba
            or new_geometry.zone_lba % old_geometry.zone_lba
            or new_geometry.capacity > old_geometry.capacity):
        raise ValueError('coarsening requires stripe=zone, an integer size multiple, and non-increasing capacity')
    factor = new_geometry.zone_lba // old_geometry.zone_lba
    block = (1 << factor) - 1
    output = []
    for source in records:
        row = copy.deepcopy(source)
        old_mask = int(source['coverage']['unfolded']['mask_hex'], 16)
        new_mask = 0
        for zone in range(new_geometry.user_zones):
            if old_mask >> (zone * factor) & block:
                new_mask |= 1 << zone
        # Only the no-folding footprint is valid for the target configuration.
        row['coverage'] = {'unfolded': {'used_zones': new_mask.bit_count(), 'mask_hex': hex(new_mask)}}
        row['derived_from_raw_zone_size_lba'] = old_geometry.zone_lba
        output.append(row)
    return output


def solve(records, zone_count, mode='read_write', strict=False):
    candidates = [r for r in records if r['valid_for_native_replay']
                  and (not strict or r['runtime_remapped_requests'] == 0)]
    masks = [int(r['coverage'][mode]['mask_hex'], 16) for r in candidates]
    costs = [r['requests'] for r in candidates]
    target = (1 << zone_count) - 1
    union = 0
    for mask in masks:
        union |= mask
    result = {'mode': mode, 'strict_no_runtime_folding': strict, 'candidate_count': len(candidates),
              'all_candidates_used_zones': union.bit_count(),
              'unreachable_zone_ids': [z for z in range(zone_count) if not union >> z & 1]}
    if union != target:
        return {**result, 'status': 'INFEASIBLE', 'proof': 'union_of_all_candidates_leaves_uncovered_zones'}
    # Each zone imposes a hitting-set constraint over whole-file candidate choices.
    constraints = set()
    for z in range(zone_count):
        constraints.add(sum(1 << i for i, mask in enumerate(masks) if mask >> z & 1))
    ordered = sorted(constraints, key=lambda v: (v.bit_count(), v))
    minimal = []
    for mask in ordered:
        if not any(existing & mask == existing for existing in minimal):
            minimal.append(mask)
    best_cost = sum(costs) + 1
    best_selection = None
    nodes = 0
    cache = {}

    def search(remaining, chosen, cost):
        nonlocal best_cost, best_selection, nodes
        nodes += 1
        if cost >= best_cost:
            return
        if not remaining:
            best_cost, best_selection = cost, chosen
            return
        if cache.get(remaining, best_cost) <= cost:
            return
        cache[remaining] = cost
        constraint = min(remaining, key=int.bit_count)
        choices = [i for i in range(len(costs)) if constraint >> i & 1]
        choices.sort(key=lambda i: costs[i] / sum(bool(c >> i & 1) for c in remaining))
        # Every remaining constraint needs at least its cheapest available file.
        lower = max(min(costs[i] for i in range(len(costs)) if c >> i & 1) for c in remaining)
        if cost + lower >= best_cost:
            return
        for i in choices:
            search(tuple(c for c in remaining if not c >> i & 1), chosen | (1 << i), cost + costs[i])

    search(tuple(minimal), 0, 0)
    selected = [r for i, r in enumerate(candidates) if best_selection >> i & 1]
    verified = 0
    for r in selected:
        verified |= int(r['coverage'][mode]['mask_hex'], 16)
    assert verified == target and sum(r['requests'] for r in selected) == best_cost
    return {**result, 'status': 'OPTIMAL', 'objective_total_requests': best_cost,
            'selected': [{'name': r['name'], 'path': r['path'], 'requests': r['requests'],
                          'devices': r.get('devices', []),
                          'used_zones': r['coverage'][mode]['used_zones'],
                          'runtime_remapped_requests': r['runtime_remapped_requests']} for r in selected],
            'proof': 'exhaustive_branch_and_bound_over_minimal_zone_membership_constraints',
            'original_constraints': len(constraints), 'minimal_constraints': len(minimal), 'search_nodes': nodes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssd', required=True, type=Path)
    parser.add_argument('--swans', required=True, type=Path)
    parser.add_argument('--root', required=True, nargs='+', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--native-parser', type=Path, help='optional compiled fast CSV parser')
    parser.add_argument('--no-folding', action='store_true', help='forbid address folding and require whole original files to fit')
    parser.add_argument('--coarsen-cache', type=Path, help='reuse smaller stripe=zone raw footprints for a larger no-folding zone')
    args = parser.parse_args()
    g, _ = load_geometry(args.ssd, args.swans)
    args.out.mkdir(parents=True, exist_ok=True)
    paths = sorted(set(p.resolve() for root in args.root for p in root.rglob('*.csv')))
    excluded = [str(p) for p in paths if 'compact' in p.name.lower()]
    paths = [p for p in paths if 'compact' not in p.name.lower()]
    originals = {p.with_name(p.stem.removesuffix('_original') + p.suffix) for p in paths if p.stem.endswith('_original')}
    alternate_versions = [str(p) for p in paths if p in originals]
    paths = [p for p in paths if p not in originals]
    records = []
    cache_hits = 0
    start = time.monotonic()
    fingerprint = hashlib.sha256((args.ssd.read_bytes() + args.swans.read_bytes())).hexdigest()
    coarsened = {}
    if args.coarsen_cache:
        if not args.no_folding:
            parser.error('--coarsen-cache requires --no-folding')
        previous = json.loads(args.coarsen_cache.read_text(encoding='utf-8'))
        coarsened = {r['path']: r for r in coarsen_no_folding_records(previous['records'], Geometry(**previous['geometry']), g)}
    for index, path in enumerate(paths, 1):
        cache_path = args.out / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + '.scan.json')
        stat = path.stat()
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else None
        reusable = coarsened.get(str(path))
        if reusable and reusable['bytes'] == stat.st_size and reusable['mtime_ns'] == stat.st_mtime_ns:
            record = reusable
            cache_hits += 1
        elif cached and cached.get('config_sha256') == fingerprint and cached['bytes'] == stat.st_size and cached['mtime_ns'] == stat.st_mtime_ns:
            record = cached
            cache_hits += 1
        else:
            record = scan_native(path, g, args.native_parser, args.no_folding) if args.native_parser else scan(path, g, args.no_folding)
            record['config_sha256'] = fingerprint
            # A no-folding-only scan cannot replace a cache that promises folded masks.
            if not args.no_folding:
                cache_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
        records.append(record)
        if not args.no_folding:
            print(f"{index}/{len(paths)} {path.name}: requests={record['requests']:,}, zones={record['coverage']['read_write']['used_zones']}/{g.user_zones}, remapped={record['runtime_remapped_requests']:,}, native_valid={record['valid_for_native_replay']}; elapsed={time.monotonic()-start:.1f}s", flush=True)
    if args.no_folding:
        result = no_folding_result(records, g)
        result.update(config_sha256=fingerprint, cache_hits=cache_hits, source_file_count=len(records),
                      elapsed_seconds=time.monotonic()-start, geometry=asdict(g),
                      coarsened_from_cache=str(args.coarsen_cache) if args.coarsen_cache else None)
        (args.out / 'no-folding-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (args.out / 'no-folding-empty-zones.txt').write_text(''.join(f'{z}\n' for z in result['unreachable_zone_ids']), encoding='utf-8')
        print(json.dumps({k: result[k] for k in ['status','candidate_count','all_candidates_used_zones','cache_hits','elapsed_seconds','per_ssd']}, indent=2))
        return
    output = {'geometry': asdict(g), 'config_sha256': fingerprint, 'excluded_compact': excluded,
              'excluded_alternate_versions_with_explicit_original': alternate_versions,
              'file_count': len(records), 'records': records, 'results': []}
    for mode, strict in [('read_write', False), ('read_write', True), ('writes', False)]:
        result = solve(records, g.user_zones, mode, strict)
        output['results'].append(result)
        print(f"{mode}, no_folding={strict}: {result['status']}, union zones={result['all_candidates_used_zones']}, cost={result.get('objective_total_requests')}", flush=True)
    # The user's primary objective is address footprint, independent of SSD I/O execution.
    # Keep native-replay eligibility as a separate report, never hide its boundary errors.
    footprint_records = [dict(r, valid_for_native_replay=True) for r in records]
    footprint = solve(footprint_records, g.user_zones)
    footprint['native_replay_warnings'] = [{'name': r['name'], 'crosses_capacity': r['crosses_capacity'],
                                           'oversized_requests': r['oversized_requests'],
                                           'out_of_order_timestamps': r['out_of_order_timestamps']}
                                          for r in records if not r['valid_for_native_replay']
                                          and r['name'] in {s['name'] for s in footprint.get('selected', [])}]
    output['address_footprint_optimum'] = footprint
    print(f"Address footprint: {footprint['status']}; cost={footprint.get('objective_total_requests')}", flush=True)
    (args.out / 'combination-results.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
