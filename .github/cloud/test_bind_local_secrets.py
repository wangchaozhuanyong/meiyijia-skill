import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


spec = importlib.util.spec_from_file_location('binder', Path(__file__).with_name('bind_local_secrets.py'))
binder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binder)


class BindingTests(unittest.TestCase):
    def test_existing_env_is_read_without_executing_shell_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('\n'.join(f'{name}="literal-$(not-a-command)"' for name in binder.NAMES))
            result = binder.read_credentials(path)
            self.assertEqual(result[binder.NAMES[0]], 'literal-$(not-a-command)')

    def test_placeholder_source_is_rejected_without_revealing_other_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('POSPAL_USERNAME=private-fixture\nPOSPAL_PASSWORD=your-password\n')
            with self.assertRaises(RuntimeError) as error:
                binder.read_credentials(path)
            self.assertNotIn('private-fixture', str(error.exception))
            self.assertIn('POSPAL_PASSWORD', str(error.exception))

    def test_values_go_only_to_stdin_and_existing_bindings_are_preserved(self):
        existing = {binder.NAMES[0]}
        credentials = {name: 'sensitive-fixture-' + name for name in binder.NAMES}
        writes = []
        def run(command, **kwargs):
            if command[2] == 'list':
                return SimpleNamespace(returncode=0, stdout=json.dumps([{'name': n} for n in existing]).encode())
            name = command[3]
            self.assertFalse(any(value in ' '.join(command) for value in credentials.values()))
            self.assertEqual(kwargs['input'], credentials[name].encode())
            self.assertNotIn(name, existing)
            existing.add(name)
            writes.append(name)
            return SimpleNamespace(returncode=0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(binder.bind(credentials, run), (3, 1))
        self.assertEqual(len(writes), 3)
        for value in credentials.values():
            self.assertNotIn(value, output.getvalue())

    def test_permission_failure_does_not_attempt_writes_or_print_cli_output(self):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'do-not-show-response')
        with self.assertRaises(RuntimeError) as error:
            binder.bind({name: 'fixture' for name in binder.NAMES}, run)
        self.assertEqual(len(commands), 1)
        self.assertNotIn('do-not-show-response', str(error.exception))


if __name__ == '__main__':
    unittest.main()
