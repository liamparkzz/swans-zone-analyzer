from itertools import combinations
from pathlib import Path
import json
import random
import subprocess
import sys
import tempfile
import unittest

from find_trace_combination import scan, scan_native, solve, no_folding_result, coarsen_no_folding_records
from zone_check import Geometry, mark_request


def record(name, mask, cost, remapped=0, valid=True):
    return {'name': name, 'path': name, 'requests': cost, 'runtime_remapped_requests': remapped,
            'valid_for_native_replay': valid,
            'coverage': {mode: {'mask_hex': hex(mask), 'used_zones': mask.bit_count()}
                         for mode in ['read_write', 'writes']}}


class CombinationTests(unittest.TestCase):
    def test_coarsening_matches_direct_raw_scan_with_new_stripe_and_zone(self):
        old = Geometry(4, 8, 8, 128, 120, 15, True, 'NVME')
        new = Geometry(4, 16, 16, 128, 112, 7, True, 'NVME')
        rng = random.Random(32116)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'original.csv'
            for _ in range(50):
                requests = []
                for i in range(rng.randint(1, 10)):
                    lba = rng.randrange(new.capacity)
                    sectors = rng.randint(1, min(25, new.capacity - lba))
                    requests.append(f'{i},host,0,Write,{lba*512},{sectors*512},1\n')
                source.write_text(''.join(requests))
                cached = scan(source, old, no_folding=True)
                actual = coarsen_no_folding_records([cached], old, new)[0]
                expected = scan(source, new, no_folding=True)
                self.assertEqual(actual['coverage']['unfolded'], expected['coverage']['unfolded'])
                self.assertEqual(actual['requests'], expected['requests'])
            # Adjacent old RAID stripes can belong to different SSDs; coarsening
            # must merge raw-address IDs, not pair zones separately inside each SSD.
            source.write_text('0,host,0,Write,4096,512,1\n')
            cached = scan(source, old, no_folding=True)
            actual = coarsen_no_folding_records([cached], old, new)[0]
            self.assertEqual(int(cached['coverage']['unfolded']['mask_hex'], 16), 2)
            self.assertEqual(int(actual['coverage']['unfolded']['mask_hex'], 16), 1)

    def test_coarsening_rejects_larger_capacity_or_non_multiple_size(self):
        old = Geometry(4, 8, 8, 128, 120, 15, True, 'NVME')
        with self.assertRaises(ValueError):
            coarsen_no_folding_records([], old, Geometry(4, 16, 16, 128, 128, 8, False, 'NVME'))
        with self.assertRaises(ValueError):
            coarsen_no_folding_records([], old, Geometry(4, 12, 12, 128, 108, 9, True, 'NVME'))

    def test_repeated_zone_requests_only_update_each_zone_once(self):
        g = Geometry(1, 64, 64, 256, 256, 4, False, 'NVME')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'original.csv'
            path.write_text('0,h,0,Write,0,512,1\n1,h,0,Write,0,512,1\n'
                            '2,h,0,Write,32256,1024,1\n3,h,0,Read,32768,512,1\n')
            result = scan(path, g, no_folding=True)
        self.assertEqual(result['requests'], 4)
        self.assertEqual(result['zone_state_updates'], 2)
        self.assertEqual(result['zone_new_requests'], 2)
        self.assertEqual(result['zone_skip_requests'], 2)
        self.assertEqual(result['coverage']['read_write']['used_zones'], 2)

    def test_no_folding_never_maps_outside_requests(self):
        g = Geometry(1, 64, 64, 256, 256, 4, False, 'NVME')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'original.csv'
            path.write_text('0,h,0,Write,0,512,1\n1,h,0,Write,262144,512,1\n')
            result = scan(path, g, no_folding=True)
        self.assertEqual(result['requests'], 2)
        self.assertEqual(result['out_of_range_requests'], 1)
        self.assertEqual(result['runtime_remapped_requests'], 0)
        self.assertEqual(result['coverage']['read_write']['used_zones'], 1)
        self.assertEqual(result['zone_state_updates'], 1)

    def test_no_folding_rejects_whole_file_instead_of_deleting_requests(self):
        g = Geometry(1, 64, 64, 320, 256, 4, True, 'NVME')
        good = record('good', 1, 10)
        bad = record('outside', 15, 1)
        for row in [good, bad]:
            row.update(oversized_requests=0, out_of_order_timestamps=0)
            row['coverage']['unfolded'] = row['coverage']['read_write']
        good['max_original_end_bytes'] = 512
        bad['max_original_end_bytes'] = g.capacity * 512 + 1
        result = no_folding_result([good, bad], g)
        self.assertEqual(result['status'], 'INFEASIBLE')
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(result['all_candidates_used_zones'], 1)
        self.assertEqual(result['reserve_zone_count_excluded'], 1)
        self.assertEqual(result['unreachable_zone_ids'], [1, 2, 3])

    def test_cli_prefers_explicit_original_over_ambiguous_alternate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ['prn_0_original.csv', 'prn_0.csv', 'prn_0_compact.csv']:
                (root / name).write_text('0,prn,0,Write,0,512,1\n')
            base = Path(__file__).parent
            preset = base / 'configs/32gibx4-zone16mib'
            run = subprocess.run([sys.executable, str(base / 'find_trace_combination.py'),
                                  '--ssd', str(preset / 'ssdconfig.xml'), '--swans', str(preset / 'swansconfig.xml'),
                                  '--root', str(root), '--out', str(root / 'report')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads((root / 'report/combination-results.json').read_text(encoding='utf-8'))
            self.assertEqual(report['file_count'], 1)
            self.assertEqual(report['records'][0]['name'], 'prn_0_original.csv')
            self.assertEqual(len(report['excluded_compact']), 1)
            self.assertEqual(len(report['excluded_alternate_versions_with_explicit_original']), 1)

    @unittest.skipUnless(Path(__file__).with_name('msrc_scan_native.exe').exists(), 'optional native parser not built')
    def test_native_parser_matches_python_across_chunk_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'original.csv'
            source.write_bytes(b'0,h,0,Write,0,512,1\r\n' * 220000 + b'1,h,0,Read,512,512,1')
            g = Geometry(1, 64, 64, 128, 128, 2, False, 'NVME')
            expected = json.loads(json.dumps(scan(source, g)))
            actual = scan_native(source, g, Path(__file__).with_name('msrc_scan_native.exe'))
            self.assertEqual(actual, expected)
            self.assertEqual(actual['zone_state_updates'], 1)
            self.assertEqual(actual['zone_skip_requests'], 220000)
            source.write_bytes(b'0,h,0,Write,0,512,1\n1,h,0,Read,65536,512,1\n')
            expected = json.loads(json.dumps(scan(source, g, no_folding=True)))
            actual = scan_native(source, g, Path(__file__).with_name('msrc_scan_native.exe'), no_folding=True)
            self.assertEqual(actual, expected)
            self.assertEqual(actual['runtime_remapped_requests'], 0)
            self.assertEqual(actual['out_of_range_requests'], 1)

    def test_minimize_requests_instead_of_number_of_files(self):
        data = [record('expensive', 15, 100), record('first', 3, 9), record('second', 12, 10)]
        result = solve(data, 4)
        self.assertEqual(result['status'], 'OPTIMAL')
        self.assertEqual(result['objective_total_requests'], 19)
        self.assertEqual({r['name'] for r in result['selected']}, {'first', 'second'})

    def test_union_proves_infeasibility(self):
        result = solve([record('a', 3, 1), record('b', 4, 2)], 4)
        self.assertEqual(result['status'], 'INFEASIBLE')
        self.assertEqual(result['unreachable_zone_ids'], [3])

    def test_strict_and_native_validity_filters(self):
        data = [record('folded', 15, 1, remapped=2), record('invalid', 15, 1, valid=False),
                record('preserved', 15, 8)]
        self.assertEqual(solve(data, 4)['objective_total_requests'], 1)
        self.assertEqual(solve(data, 4, strict=True)['objective_total_requests'], 8)

    def test_matches_exhaustive_subsets_on_random_instances(self):
        rng = random.Random(51119)
        for _ in range(100):
            zones = rng.randint(1, 12)
            data = [record(str(i), rng.randrange(1 << zones), rng.randint(1, 500))
                    for i in range(rng.randint(1, 10))]
            best = None
            for size in range(1, len(data) + 1):
                for subset in combinations(data, size):
                    union = 0
                    for r in subset:
                        union |= int(r['coverage']['read_write']['mask_hex'], 16)
                    if union == (1 << zones) - 1:
                        cost = sum(r['requests'] for r in subset)
                        best = cost if best is None else min(best, cost)
            actual = solve(data, zones)
            self.assertEqual(actual['status'], 'INFEASIBLE' if best is None else 'OPTIMAL')
            self.assertEqual(actual.get('objective_total_requests'), best)

    def test_raw_scan_agrees_with_normal_analyzer(self):
        g = Geometry(4, 64, 64, 192, 128, 2, True, 'NVME')
        requests = [(0, 8, 0), (64, 8, 1), (252, 8, 0), (1024, 8, 1)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'original.csv'
            path.write_text(''.join(f'{i},host,0,{"Write" if op == 0 else "Read"},{lba*512},{size*512},1\n'
                                    for i, (lba, size, op) in enumerate(requests)))
            result = scan(path, g)
        expected = bytearray(g.total_zones)
        for lba, size, op in requests:
            mark_request(expected, g, lba, size)
        actual = int(result['coverage']['read_write']['mask_hex'], 16)
        self.assertEqual(actual, sum(1 << i for i, v in enumerate(expected) if v))
        self.assertEqual(result['runtime_remapped_requests'], 1)
        self.assertEqual(result['requests'], 4)
        self.assertEqual(actual >> g.user_zones, 0)
        self.assertEqual(result['coverage']['unfolded']['used_zones'], 4)


if __name__ == '__main__':
    unittest.main()
