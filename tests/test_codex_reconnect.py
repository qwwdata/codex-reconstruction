"""Safety and behavior tests use temporary files and synthetic logs only."""
from contextlib import closing, redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_reconnect.py"
spec = importlib.util.spec_from_file_location("codex_reconnect", SCRIPT)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class TomlPatchTests(unittest.TestCase):
    def check_change(self, original):
        if isinstance(original, str):
            original = original.encode("utf-8")
        expected = copy.deepcopy(repair.parse_config(original))
        expected.setdefault("features", {})["respect_system_proxy"] = True
        candidate = repair.patch_system_proxy(original)
        self.assertEqual(repair.parse_config(candidate), expected)
        self.assertEqual(repair.patch_system_proxy(candidate), candidate)
        return candidate

    def test_empty_config(self):
        self.check_change(b"")

    def test_append_preserves_unrelated_settings(self):
        original = ('model = "example-model"\nmodel_reasoning_effort = "high"\n'
                    '[mcp_servers.demo]\ncommand = "demo"\n'
                    '[profiles.example]\nmodel_provider = "company"\n'
                    '[model_providers.company]\nbase_url = "https://example.invalid/v1"\n'
                    'env_key = "EXAMPLE_KEY"\n')
        candidate = self.check_change(original)
        self.assertTrue(candidate.startswith(original.encode()))

    def test_existing_table_inserts_before_next_table(self):
        candidate = self.check_change('[features] # keep\napps = true\n[other]\nx = 3\n')
        self.assertIn(b'[features] # keep\nrespect_system_proxy = true\napps = true', candidate)

    def test_false_replaced_preserving_comment_and_spacing(self):
        candidate = self.check_change('[features]\n  respect_system_proxy  = false  # keep me\n')
        self.assertIn(b'  respect_system_proxy  = true  # keep me\n', candidate)

    def test_bom_crlf_and_chinese(self):
        candidate = self.check_change('\ufeff# 中文注释\r\n[features]\r\nrespect_system_proxy = false\r\n'.encode('utf-8'))
        self.assertTrue(candidate.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\n', candidate.replace(b'\r\n', b''))
        self.assertIn('中文注释'.encode(), candidate)

    def test_dotted_features(self):
        self.check_change('model = "example"\nfeatures.apps = true\n[other]\nx = 1\n')

    def test_dotted_existing_flag(self):
        candidate = self.check_change('features.respect_system_proxy = false # keep\n')
        self.assertEqual(candidate, b'features.respect_system_proxy = true # keep\n')

    def test_quoted_table_and_key(self):
        self.check_change('["features"]\n"respect_system_proxy" = false\n')

    def test_multiline_fake_table_and_assignment_are_preserved(self):
        original = """notes = '''
[features]
respect_system_proxy = false
'''
[features]
apps = false
"""
        candidate = self.check_change(original)
        self.assertEqual(repair.parse_config(candidate)['notes'], repair.parse_config(original.encode())['notes'])

    def test_multiline_basic_string_with_escaped_quotes(self):
        self.check_change('notes = """example \\" text\n[features]\n"""\n[features]\napps = true\n')

    def test_no_final_newline(self):
        self.check_change('[features]')
        self.check_change('features.apps = true')
        self.check_change('model = "example"')

    def test_inline_table_refuses_without_mutation(self):
        with self.assertRaises(repair.RepairError):
            repair.patch_system_proxy(b'features = { apps = true }\n')

    def test_already_enabled_is_byte_identical(self):
        original = b'features = { respect_system_proxy = true } # keep\n'
        self.assertEqual(repair.patch_system_proxy(original), original)

    def test_invalid_toml_or_flag_type_refuses(self):
        for original in (b'[bad', b'features = 3', b'[features]\nrespect_system_proxy = "false"'):
            with self.subTest(original=original), self.assertRaises(repair.RepairError):
                repair.patch_system_proxy(original)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'config.toml'
        self.original = '\ufeff# 中文\r\nmodel = "example"\r\n'.encode('utf-8')
        self.path.write_bytes(self.original)
        self.candidate = repair.patch_system_proxy(self.original)

    def apply(self):
        report = repair.apply_patch(self.path, self.original, self.candidate, dry_run=False)
        return Path(report['backup'])

    def test_dry_run_creates_no_files(self):
        report = repair.apply_patch(self.path, self.original, self.candidate, dry_run=True)
        self.assertEqual(report['status'], 'dry_run')
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_round_trip_and_idempotence(self):
        backup = self.apply()
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(self.path.read_bytes(), self.candidate)
        second = repair.apply_patch(self.path, self.candidate, self.candidate, dry_run=False)
        self.assertEqual(second['status'], 'already_enabled')
        repair.restore_backup(self.path, backup, dry_run=True)
        self.assertEqual(self.path.read_bytes(), self.candidate)
        repair.restore_backup(self.path, backup, dry_run=False)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(repair.restore_backup(self.path, backup, dry_run=False)['status'], 'already_restored')

    def test_restore_preserves_later_edits(self):
        backup = self.apply()
        edited = self.candidate + b'\n[extra]\nvalue = 1\n'
        self.path.write_bytes(edited)
        with self.assertRaisesRegex(repair.RepairError, 'changed since'):
            repair.restore_backup(self.path, backup, dry_run=False)
        self.assertEqual(self.path.read_bytes(), edited)

    def test_tampered_backup_refuses(self):
        backup = self.apply()
        backup.write_bytes(b'# tampered\n')
        with self.assertRaisesRegex(repair.RepairError, 'checksum'):
            repair.restore_backup(self.path, backup, dry_run=False)

    def test_wrong_config_refuses(self):
        backup = self.apply()
        other = self.path.parent / 'other.toml'
        other.write_bytes(self.candidate)
        with self.assertRaisesRegex(repair.RepairError, 'different'):
            repair.restore_backup(other, backup, dry_run=False)

    def test_non_object_manifest_refuses(self):
        backup = self.apply()
        Path(str(backup) + '.json').write_text('[]', encoding='utf-8')
        with self.assertRaises(repair.RepairError):
            repair.restore_backup(self.path, backup, dry_run=False)

    def test_changed_config_before_apply_refuses(self):
        edited = b'# later edits\n'
        self.path.write_bytes(edited)
        with self.assertRaisesRegex(repair.RepairError, 'changed during'):
            self.apply()
        self.assertEqual(self.path.read_bytes(), edited)
        self.assertFalse((self.path.parent / 'reconnect-backups').exists())


class CapabilityTests(unittest.TestCase):
    def test_stages_and_missing_feature(self):
        output = 'other stable true\nrespect_system_proxy under development false\n'
        self.assertEqual(repair.feature_info(output), {'available': True, 'stage': 'under development', 'enabled': False})
        self.assertFalse(repair.feature_info('respect_system_proxy removed false')['available'])
        self.assertFalse(repair.feature_info('responses_websockets removed false')['available'])

    def test_disabled_proxy_pac_and_removed_feature_refuse(self):
        feature = {'available': True}
        static = {'platform': 'Windows', 'enabled': True, 'static_proxy': True, 'pac': False}
        repair.require_system_proxy(feature, static)
        for invalid in ({**static, 'enabled': False}, {**static, 'static_proxy': False, 'pac': True}, {**static, 'platform': 'non-Windows'}):
            with self.assertRaises(repair.RepairError):
                repair.require_system_proxy(feature, invalid)
        with self.assertRaises(repair.RepairError):
            repair.require_system_proxy({'available': False}, static)

    def test_cli_apply_dry_run_with_mocked_windows_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.toml'
            config.write_text('model = "example"\n', encoding='utf-8')
            def output(_, arguments):
                return 'codex-cli 0.162.0-alpha.2\n' if arguments == ['--version'] else 'respect_system_proxy under development false\n'
            with patch.object(repair, 'run_codex', side_effect=output), patch.object(repair, 'windows_proxy', return_value={'platform': 'Windows', 'enabled': True, 'static_proxy': True}), redirect_stdout(io.StringIO()) as stream:
                result = repair.main(['apply-system-proxy', '--config', str(config), '--codex', 'fake', '--dry-run'])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stream.getvalue())['status'], 'dry_run')
            self.assertEqual(list(config.parent.iterdir()), [config])


class LogTests(unittest.TestCase):
    def test_bounded_counts_thread_filter_and_no_private_output(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'logs_2.sqlite'
            now = int(time.time())
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, target TEXT, feedback_log_body TEXT, thread_id TEXT)')
                rows = [
                    (now, 'codex_api::endpoint::responses_websocket', 'connecting to websocket PRIVATE_PROMPT', 'affected'),
                    (now, 'codex_core::responses_retry', 'retrying sampling request 1/5 TLS close_notify SECRET_TOKEN', 'affected'),
                    (now, 'codex_core::client', 'falling back to HTTP', 'affected'),
                    (now, 'feedback_tags', 'transport="responses_http"', 'affected'),
                    (now, 'network', 'request timed out', 'unrelated'),
                    (now - 1000, 'network', 'request timed out', 'affected'),
                ]
                connection.executemany('INSERT INTO logs (ts,target,feedback_log_body,thread_id) VALUES (?,?,?,?)', rows)
                connection.commit()
            report = repair.summarize_logs(database, minutes=5, thread_id='affected')
            self.assertEqual(report['scanned_events'], 4)
            self.assertEqual(report['counts']['websocket_attempts'], 1)
            self.assertEqual(report['counts']['stream_retry_events'], 1)
            self.assertEqual(report['counts']['http_fallbacks'], 1)
            self.assertEqual(report['counts']['tls_unexpected_eof_events'], 1)
            self.assertEqual(report['counts']['timeout_events'], 0)
            for private in ('PRIVATE_PROMPT', 'SECRET_TOKEN', 'affected'):
                self.assertNotIn(private, json.dumps(report))
            all_chats = repair.summarize_logs(database, minutes=5, thread_id=None)
            self.assertEqual(all_chats['counts']['timeout_events'], 1)

    def test_live_wal_is_included_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'logs_2.sqlite'
            connection = sqlite3.connect(database)
            self.addCleanup(connection.close)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, target TEXT, feedback_log_body TEXT, thread_id TEXT)')
            connection.commit()
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            connection.execute('INSERT INTO logs VALUES (1,?,?,?,?)', (int(time.time()), 'network', 'request timed out', 'sample'))
            connection.commit()
            report = repair.summarize_logs(database, minutes=1, thread_id=None)
            self.assertEqual(report['counts']['timeout_events'], 1)
            self.assertTrue(Path(str(database) + '-wal').exists())
            connection.close()

    def test_bad_schema_reports_safe_error(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'logs_2.sqlite'
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('CREATE TABLE secret (token TEXT)')
            with redirect_stderr(io.StringIO()) as stream:
                result = repair.main(['check-logs', '--logs', str(database)])
            self.assertEqual(result, 1)
            self.assertIn('Unsupported log schema', stream.getvalue())
            self.assertNotIn('secret', stream.getvalue())


if __name__ == '__main__':
    unittest.main()

