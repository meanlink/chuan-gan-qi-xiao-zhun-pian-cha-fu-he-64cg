"""温标核心逻辑单测（纯标准库，无需 SQLAlchemy/FastAPI）。

覆盖：温标别名与未知温标拒绝、绝对零度边界、华氏/开尔文换算精度、
逐条声明与旧接入兼容、原值与换算依据保留、分析视图只含摄氏、混合批次语义。
可直接 `python tests/test_temperature.py` 运行，也可由 unittest discover 收集。
"""
import sys
import os
import json
import unittest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from src.services.temperature import (
    to_celsius, normalize_scale, normalize_record, normalize_records,
    analysis_views, TemperatureError, conversion_basis,
)


class TemperatureTests(unittest.TestCase):
    def test_scale_aliases_and_unknown_rejection(self):
        self.assertEqual(normalize_scale('c'), 'CELSIUS')
        self.assertEqual(normalize_scale('摄氏'), 'CELSIUS')
        self.assertEqual(normalize_scale('°C'), 'CELSIUS')
        self.assertEqual(normalize_scale('f'), 'FAHRENHEIT')
        self.assertEqual(normalize_scale('华氏'), 'FAHRENHEIT')
        self.assertEqual(normalize_scale('k'), 'KELVIN')
        self.assertEqual(normalize_scale('开尔文'), 'KELVIN')
        for bad in ('R', 'Rankine', '', None):
            with self.assertRaises(TemperatureError):
                normalize_scale(bad)

    def test_conversion_precision(self):
        self.assertEqual(to_celsius(25.0, 'C'), 25.0)
        self.assertAlmostEqual(to_celsius(32.0, 'F'), 0.0, places=12)
        self.assertAlmostEqual(to_celsius(212.0, 'F'), 100.0, places=12)
        self.assertAlmostEqual(to_celsius(-40.0, 'F'), -40.0, places=12)
        self.assertAlmostEqual(to_celsius(273.15, 'K'), 0.0, places=12)
        self.assertAlmostEqual(to_celsius(373.15, 'K'), 100.0, places=12)
        self.assertAlmostEqual(to_celsius(0, 'K'), -273.15, places=12)
        self.assertAlmostEqual(to_celsius(98.6, 'F'), 37.0, places=9)

    def test_non_finite_and_boolean_rejected(self):
        with self.assertRaises(TemperatureError):
            to_celsius(float('nan'), 'C')
        with self.assertRaises(TemperatureError):
            to_celsius('hot', 'C')
        with self.assertRaises(TemperatureError):
            to_celsius(True, 'C')

    def test_absolute_zero_boundary(self):
        with self.assertRaises(TemperatureError):
            to_celsius(-273.16, 'C')
        with self.assertRaises(TemperatureError):
            to_celsius(-0.01, 'K')
        with self.assertRaises(TemperatureError):
            to_celsius(-459.68, 'F')
        self.assertEqual(to_celsius(-273.15, 'C'), -273.15)
        self.assertAlmostEqual(to_celsius(-459.67, 'F'), -273.15, places=9)

    def test_legacy_intake_is_celsius_with_basis(self):
        legacy = normalize_record({'sample': 'S1', 'band': '常温',
                                   'reference': 25.0, 'measured': 25.4})
        self.assertEqual(legacy['scale_mode'], 'legacy_celsius')
        self.assertEqual(legacy['reference'], 25.0)
        self.assertTrue(legacy['conversion']['reference']['formula'].startswith('legacy'))
        with self.assertRaises(TemperatureError):
            normalize_record({'sample': 'S', 'band': 'b', 'reference': -300, 'measured': 1})
        with self.assertRaises(TemperatureError):
            normalize_record({'sample': 'S', 'band': 'b', 'reference': 'x', 'measured': 1})

    def test_explicit_intake_converts_and_preserves_originals(self):
        row = {'sample': 'S2', 'band': '高温', 'reference': 300.0, 'reference_scale': 'K',
               'measured': 80.6, 'measured_scale': 'F'}
        new = normalize_record(row, 0)
        self.assertEqual(new['scale_mode'], 'explicit_scale')
        self.assertAlmostEqual(new['reference'], 26.85, places=12)
        self.assertAlmostEqual(new['measured'], 27.0, places=12)
        self.assertEqual(new['reference_original'], 300.0)
        self.assertEqual(new['reference_scale'], 'KELVIN')
        self.assertEqual(new['measured_original'], 80.6)
        self.assertEqual(new['measured_scale'], 'FAHRENHEIT')
        self.assertEqual(new['conversion']['reference']['formula'],
                         'celsius = value - 273.15')
        self.assertEqual(new['conversion']['measured']['original_value'], 80.6)
        self.assertEqual(new['conversion']['measured']['absolute_zero_check'], 'passed')

    def test_row_level_scale_applies_to_both_fields(self):
        uni = normalize_record({'sample': 'S3', 'band': '低温', 'scale': 'F',
                                'reference': 5.0, 'measured': 4.1})
        self.assertEqual(uni['reference_scale'], 'FAHRENHEIT')
        self.assertEqual(uni['measured_scale'], 'FAHRENHEIT')
        self.assertAlmostEqual(uni['reference'], (5 - 32) * 5 / 9, places=12)

    def test_missing_and_invalid_scale_rejected(self):
        with self.assertRaises(TemperatureError):
            normalize_record({'sample': 'S', 'band': 'b', 'reference': 1,
                              'measured': 2, 'reference_scale': 'C'})
        with self.assertRaises(TemperatureError):
            normalize_records([
                {'sample': 'a', 'band': 'b', 'reference': 1, 'measured': 2, 'scale': 'C'},
                {'sample': 'b', 'band': 'b', 'reference': 1, 'measured': 2, 'scale': 'R'},
            ])
        with self.assertRaises(TemperatureError):
            normalize_records([
                {'sample': 'a', 'band': 'b', 'reference': 1, 'measured': 2, 'scale': 'K'},
                {'sample': 'b', 'band': 'b', 'reference': -1, 'measured': 2, 'scale': 'K'},
            ])

    def test_analysis_view_is_celsius_only(self):
        new = normalize_record({'sample': 'S2', 'band': '高温', 'reference': 300.0,
                                'reference_scale': 'K', 'measured': 80.6, 'measured_scale': 'F'})
        view = analysis_views([new])[0]
        self.assertEqual(set(view.keys()), {'sample', 'band', 'reference', 'measured'})
        self.assertNotIn('conversion', view)
        self.assertIsInstance(view['reference'], float)

    def test_conversion_basis_snapshot_is_json_serialisable(self):
        basis = conversion_basis(300.0, 'K', to_celsius(300.0, 'K'))
        self.assertEqual(set(basis), {'original_value', 'original_scale', 'scale_label',
                                      'formula', 'absolute_zero_check', 'celsius_value'})
        json.dumps(basis, ensure_ascii=False)

    def test_mixed_batch_keeps_distinct_modes(self):
        mixed = normalize_records([
            {'sample': 'a', 'band': 'b', 'reference': 20, 'measured': 21},
            {'sample': 'b', 'band': 'b', 'reference': 68, 'measured': 69, 'scale': 'F'},
        ])
        self.assertEqual(mixed[0]['scale_mode'], 'legacy_celsius')
        self.assertEqual(mixed[1]['scale_mode'], 'explicit_scale')
        self.assertAlmostEqual(analysis_views(mixed)[1]['reference'], 20.0, places=12)


if __name__ == '__main__':
    unittest.main(verbosity=2)
