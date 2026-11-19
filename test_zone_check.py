import csv
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from zone_check import Geometry, analyze, load_geometry, mark_request, settings


class ZoneCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        root = ET.Element('Execution_Parameter_Set')
        device = ET.SubElement(root, 'Device_Parameter_Set')
        for key, value in {'Enabled_Preconditioning': 'false', 'HostInterface_Type': 'NVME',
                           'SSD_Count': 4, 'Flash_Channel_Count': 1, 'Chip_No_Per_Channel': 1,
                           'Overprovisioning_Ratio': 0.3, 'Stripe_Unit_LBA': 64,
                           'SWANS_Zone_Size_LBA': 64, 'Zone_Stripe_Multiplier': 2}.items():
            ET.SubElement(device, key).text = str(value)
        flash = ET.SubElement(device, 'Flash_Parameter_Set')
        for key, value in {'Die_No_Per_Chip': 1, 'Plane_No_Per_Die': 1, 'Block_No_Per_Plane': 128,
                           'Page_No_Per_Block': 8, 'Page_Capacity': 4096}.items():
            ET.SubElement(flash, key).text = str(value)
        self.ssd = self.folder / 'ssd.xml'
        ET.ElementTree(root).write(self.ssd)
        self.trace = self.folder / 'io.trace'
        self.trace.write_text('0 0 0 8 0\n1 0 0 8 0\n2 0 64 8 1\n3 0 256 8 0\n4 0 252 8 0\n5 0 320 8 1\n')

    def policy(self, values):
        root = ET.Element('SWANS_Parameter_Set')
        for key, value in values.items():
            ET.SubElement(root, key).text = str(value)
        path = self.folder / 'swans.xml'
        ET.ElementTree(root).write(path)
        return path

    def test_known_trace_matches_prior_swans_zone64_reference(self):
        geometry, percent = load_geometry(self.ssd)
        self.assertEqual(geometry.backend_sectors_per_ssd, 5734)
        self.assertEqual(geometry.user_zones, 360)
        used, stats = analyze(geometry, [self.trace], percent=percent)
        self.assertEqual({i for i, v in enumerate(used) if v}, {0, 1, 3, 4, 5})
        self.assertEqual(stats['read_requests'], 2)

    def test_overlay_and_reserve_matches_prior_swans_zone128_reference(self):
        policy = self.policy({'SWANS_Zone_Size_LBA': 128, 'op_zone_migration': 'true'})
        geometry, _ = load_geometry(self.ssd, policy)
        used, _ = analyze(geometry, [self.trace])
        self.assertEqual(geometry.user_zones, 172)
        self.assertEqual(geometry.total_zones, 176)
        self.assertEqual({i for i, v in enumerate(used) if v}, {0, 1, 3})
        self.assertEqual(sum(used[geometry.user_zones:]), 0)

    def test_auto_zone_and_plane_rounding(self):
        tree = ET.parse(self.ssd)
        tree.find('.//Flash_Channel_Count').text = '3'
        tree.write(self.ssd)
        geometry, _ = load_geometry(self.ssd, self.policy({'SWANS_Zone_Size_LBA': 0}))
        self.assertEqual(geometry.zone_lba, 128)
        self.assertEqual(geometry.backend_sectors_per_ssd, 3 * 5734)

    def test_writes_only(self):
        geometry, _ = load_geometry(self.ssd)
        used, _ = analyze(geometry, [self.trace], access='writes')
        self.assertEqual({i for i, v in enumerate(used) if v}, {0, 3, 4})

    def test_no_folding_and_occupied_zone_fast_path(self):
        geometry, _ = load_geometry(self.ssd)
        used = bytearray(geometry.total_zones)
        with self.assertRaisesRegex(ValueError, 'address folding forbidden'):
            mark_request(used, geometry, geometry.capacity + 63, 8, allow_folding=False)
        self.assertEqual(sum(used), 0)
        self.assertEqual(mark_request(used, geometry, 0, 8, allow_folding=False), 1)
        self.assertEqual(mark_request(used, geometry, 0, 8, allow_folding=False), 0)

    def test_read_only_and_overwrite(self):
        self.trace.write_text('0 0 64 8 1\n1 0 64 8 1\n')
        geometry, _ = load_geometry(self.ssd)
        self.assertEqual(sum(analyze(geometry, [self.trace])[0]), 1)
        self.assertEqual(sum(analyze(geometry, [self.trace], access='writes')[0]), 0)

    def test_random_ranges_against_sector_by_sector_reference(self):
        rng = random.Random(20261119)
        for disks, stripe, zone in [(1, 8, 16), (4, 8, 32), (3, 16, 48)]:
            geometry = Geometry(disks, stripe, zone, 960, 960, 960 // zone, False, 'SATA')
            for _ in range(200):
                lba = rng.randrange(geometry.capacity)
                size = rng.randint(1, min(300, geometry.capacity - lba))
                actual = bytearray(geometry.total_zones)
                mark_request(actual, geometry, lba, size)
                expected = set()
                for sector in range(lba, lba + size):
                    local = sector // (stripe * disks) * stripe + sector % stripe
                    expected.add(local // zone * disks + sector // stripe % disks)
                self.assertEqual({i for i, value in enumerate(actual) if value}, expected)

    def test_out_of_range_remap_and_nvme_decoder(self):
        geometry = Geometry(4, 64, 64, 5734, 5734, 90, False, 'NVME')
        used = bytearray(geometry.total_zones)
        mark_request(used, geometry, geometry.capacity + 63, 8)
        self.assertEqual({i for i, v in enumerate(used) if v}, {1})
        large = Geometry(1, 64, 2**31, 2**34, 2**34, 8, False, 'NVME')
        used = bytearray(8)
        mark_request(used, large, 2**32, 8)
        self.assertEqual(list(used), [0, 1, 0, 0, 0, 0, 0, 0])

    def test_crossing_capacity_and_malformed_trace_fail(self):
        geometry, _ = load_geometry(self.ssd)
        with self.assertRaisesRegex(ValueError, 'crosses exported capacity'):
            mark_request(bytearray(geometry.total_zones), geometry, geometry.capacity - 1, 8)
        for text in ['0 0 0 8\n', '1 0 0 8 2\n', '0 0 0 65536 0\n', '2 0 0 8 0\n1 0 0 8 1\n']:
            self.trace.write_text(text)
            with self.assertRaises(ValueError):
                analyze(geometry, [self.trace])

    def test_config_errors_and_aliases(self):
        with self.assertRaisesRegex(ValueError, 'multiple of stripe'):
            load_geometry(self.ssd, self.policy({'SWANS_Zone_Size_LBA': 65}))
        tree = ET.parse(self.ssd)
        tree.find('.//Enabled_Preconditioning').text = 'true'
        tree.write(self.ssd)
        geometry, _ = load_geometry(self.ssd)
        self.assertEqual(analyze(geometry, [self.trace])[0].count(1), 5)
        for text in ['<x><op_zone_migration>true</op_zone_migration><SWANS_Reserve_Zone_Enabled>true</SWANS_Reserve_Zone_Enabled></x>',
                     '<x><SWANS_Reserve_Zone_Enabled>true</SWANS_Reserve_Zone_Enabled><op_zone_migration>true</op_zone_migration></x>']:
            self.assertEqual(settings(ET.fromstring(text)), {'op_zone_migration': 'true'})
            with self.assertRaises(ValueError):
                settings(ET.fromstring(text), strict_alias=True)

    def test_workload_resource_subset_and_percentage(self):
        tree = ET.parse(self.ssd)
        tree.find('.//Flash_Channel_Count').text = '3'
        tree.write(self.ssd)
        workload = self.folder / 'workload.xml'
        workload.write_text('<MQSim_IO_Scenarios><IO_Scenario><IO_Flow_Parameter_Set_Trace_Based>'
                            '<Channel_IDs>0</Channel_IDs><Chip_IDs>0</Chip_IDs><Die_IDs>0</Die_IDs><Plane_IDs>0</Plane_IDs>'
                            '<Relay_Count>1</Relay_Count><Percentage_To_Be_Executed>50</Percentage_To_Be_Executed>'
                            '</IO_Flow_Parameter_Set_Trace_Based></IO_Scenario></MQSim_IO_Scenarios>')
        geometry, percent = load_geometry(self.ssd, workload_path=workload)
        self.assertEqual(geometry.backend_sectors_per_ssd, 5734)
        used, stats = analyze(geometry, [self.trace], percent=percent)
        self.assertEqual(used.count(1), 2)
        self.assertEqual(stats['considered_requests'], 3)

    def test_cli_output_and_overwrite_protection(self):
        out = self.folder / 'report'
        command = [sys.executable, str(Path(__file__).with_name('zone_check.py')), '--ssd', str(self.ssd),
                   '--trace', str(self.trace), '--out', str(out)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((out / 'summary.json').read_text())
        self.assertEqual(summary['empty_user_zones'], 355)
        empty = {int(line) for line in (out / 'empty_zones.txt').read_text().splitlines()}
        self.assertEqual(empty, set(range(360)) - {0, 1, 3, 4, 5})
        with (out / 'zones.csv').open(encoding='utf-8-sig', newline='') as source:
            self.assertEqual(len(list(csv.DictReader(source))), 360)
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
