"""Offline checks for publication-safe, machine-local imagery configuration."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_up_source import resolve_source_root, SOURCE_KEY


class SourceConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / '.env.local.json'
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, value, encoding='utf-8'):
        self.config.write_text(json.dumps(value, ensure_ascii=False), encoding=encoding)

    def test_explicit_path_overrides_environment_and_invalid_local_file(self):
        os.environ[SOURCE_KEY] = r'\\env.example.invalid\imagery\test_up'
        self.config.write_text('invalid JSON', encoding='utf-8')
        self.assertEqual(resolve_source_root('  /mnt/explicit  ', config_path=self.config), '/mnt/explicit')

    def test_environment_overrides_local_file_without_opening_share(self):
        self.write({SOURCE_KEY: '/mnt/local'})
        os.environ[SOURCE_KEY] = r'\\nas.example.invalid\imagery\hebei\test_up'
        self.assertEqual(resolve_source_root(config_path=self.config), os.environ[SOURCE_KEY])

    def test_utf8_bom_local_json_and_other_settings_are_unchanged(self):
        self.write({SOURCE_KEY: '/mnt/影像/test_up', 'LOCAL_IMAGERY_DIR': 'data/imagery'}, 'utf-8-sig')
        before = self.config.read_bytes()
        self.assertEqual(resolve_source_root(config_path=self.config), '/mnt/影像/test_up')
        self.assertEqual(self.config.read_bytes(), before)

    def test_blank_environment_falls_back_to_local_file(self):
        self.write({SOURCE_KEY: '/mnt/local'})
        os.environ[SOURCE_KEY] = '  '
        self.assertEqual(resolve_source_root(config_path=self.config), '/mnt/local')

    def test_missing_configuration_has_no_implicit_network_default(self):
        with self.assertRaisesRegex(ValueError, SOURCE_KEY):
            resolve_source_root(config_path=self.config)
        self.assertFalse(self.config.exists())

    def test_configuration_without_source_key_reports_missing_value(self):
        self.write({'LOCAL_IMAGERY_DIR': 'data/imagery'})
        with self.assertRaisesRegex(ValueError, SOURCE_KEY):
            resolve_source_root(config_path=self.config)

    def test_invalid_json_error_does_not_echo_configuration_values(self):
        private_marker = 'fixture-private-value'
        self.config.write_text('{"secret": "' + private_marker + '"', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'JSON') as error:
            resolve_source_root(config_path=self.config)
        self.assertNotIn(private_marker, str(error.exception))

    def test_non_object_and_invalid_source_types_are_rejected(self):
        for value in ([], {SOURCE_KEY: 123}, {SOURCE_KEY: ''}, {SOURCE_KEY: '  '}):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(ValueError):
                    resolve_source_root(config_path=self.config)

    def test_empty_explicit_value_does_not_silently_fall_back(self):
        self.write({SOURCE_KEY: '/mnt/local'})
        for value in ('', '  '):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_source_root(value, config_path=self.config)


if __name__ == '__main__':
    unittest.main()
