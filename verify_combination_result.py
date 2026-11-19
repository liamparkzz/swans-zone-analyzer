"""Independently verify the chosen trace and exclude every cheaper whole-file subset."""
import csv
import json
from pathlib import Path
import sys
import time

from find_trace_combination import scan_native
from zone_check import load_geometry


def main():
    folder = Path(sys.argv[1])
    base = Path(__file__).parent
    preset = base / 'configs/32gibx4-zone16mib'
    g, _ = load_geometry(preset / 'ssdconfig.xml', preset / 'swansconfig.xml')
    report = json.loads((folder / 'combination-results.json').read_text(encoding='utf-8'))
    optimum = report['address_footprint_optimum']
    target = (1 << g.user_zones) - 1
    covered = 0
    source_rows = []
    for selected in optimum['selected']:
        old = next(r for r in report['records'] if r['path'] == selected['path'])
        fresh = scan_native(Path(selected['path']), g, base / 'msrc_scan_native.exe')
        assert all(old[key] == fresh[key] for key in fresh if key in old), 'Source changed or rescan mismatch'
        covered |= int(fresh['coverage']['read_write']['mask_hex'], 16)
        source_rows.append(fresh)
    assert covered == target
    upper = sum(r['requests'] for r in source_rows)
    assert upper == optimum['objective_total_requests']
    # A crossing request of <=65535 sectors can touch only the final two in-range
    # zones. Both are already present in every affected input's footprint here.
    for row in report['records']:
        assert row['oversized_requests'] == 0
        if row['crosses_capacity']:
            assert int(row['coverage']['read_write']['mask_hex'], 16) >> (g.user_zones - 2) & 3 == 3
    candidates = sorted([r for r in report['records'] if r['requests'] < upper],
                        key=lambda r: r['requests'], reverse=True)
    masks = [int(r['coverage']['read_write']['mask_hex'], 16) for r in candidates]
    costs = [r['requests'] for r in candidates]
    suffix = [0] * (len(candidates) + 1)
    for i in range(len(candidates)-1, -1, -1):
        suffix[i] = suffix[i+1] | masks[i]
    nodes = 0

    def enumerate_cheaper(index, cost, mask):
        nonlocal nodes
        nodes += 1
        if cost >= upper:
            return
        assert mask != target, 'Found a cheaper whole-file combination'
        if index == len(candidates) or mask | suffix[index] != target:
            return
        enumerate_cheaper(index+1, cost+costs[index], mask | masks[index])
        enumerate_cheaper(index+1, cost, mask)

    start = time.monotonic()
    enumerate_cheaper(0, 0, 0)
    per_ssd = []
    for disk in range(g.ssd_count):
        total = sum(bool(covered >> zone & 1) for zone in range(disk, g.user_zones, g.ssd_count))
        assert total == g.user_zones_per_ssd
        per_ssd.append({'ssd_id': disk, 'used_user_zones': total, 'empty_user_zones': 0})
    verification = {'status': 'VERIFIED', 'total_requests': upper, 'user_zones': g.user_zones,
                    'empty_user_zones': 0, 'reserve_zone_count_excluded': g.total_zones - g.user_zones,
                    'per_ssd': per_ssd, 'selected_source_rescanned_sha256': [r['sha256'] for r in source_rows],
                    'independent_subset_verification': {'cheaper_files': len(candidates), 'visited_nodes': nodes,
                                                         'cheaper_cover_exists': False,
                                                         'elapsed_seconds': time.monotonic()-start},
                    'scope': 'READ_WRITE_LOGICAL_ADDRESS_FOOTPRINT_WITH_EXISTING_MQSIM_START_LBA_FOLDING',
                    'source_files': source_rows, 'native_replay_warnings': optimum['native_replay_warnings']}
    (folder / 'verified-best-combination.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding='utf-8')
    with (folder / 'best-zones.csv').open('w', newline='', encoding='utf-8-sig') as output:
        writer = csv.writer(output)
        writer.writerow(['zone_id', 'initial_ssd_id', 'local_zone_id', 'kind', 'empty'])
        for zone in range(g.total_zones):
            writer.writerow([zone, zone % g.ssd_count, zone // g.ssd_count,
                             'user' if zone < g.user_zones else 'reserve', zone >= g.user_zones])
    (folder / 'best-empty-user-zones.txt').write_text('', encoding='utf-8')
    (folder / 'recommended-original-files.txt').write_text(''.join(r['path']+'\n' for r in source_rows), encoding='utf-8')
    print(f'Verified minimum={upper:,} requests; {g.user_zones} user zones covered; reserve excluded={g.total_zones-g.user_zones}')
    print(f'Independent cheaper-subset search: {len(candidates)} cheaper files, {nodes:,} nodes; no cheaper cover')
    print('Native execution warnings:', optimum['native_replay_warnings'])


if __name__ == '__main__':
    main()
