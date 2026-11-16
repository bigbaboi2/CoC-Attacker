import subprocess
import sys
import unittest
import uuid

from core.single_instance import SingleInstance


@unittest.skipUnless(sys.platform == 'win32', 'Windows named mutex and event')
class SingleInstanceTests(unittest.TestCase):
    def test_second_process_recalls_first_and_exits(self):
        name = f'CoCAttack.Test.{uuid.uuid4().hex}'
        first = SingleInstance(name)
        self.addCleanup(first.close)
        self.assertTrue(first.acquired)
        self.assertFalse(first.restore_requested())

        code = (
            'from core.single_instance import SingleInstance; '
            f'instance = SingleInstance({name!r}); '
            'print(instance.acquired); instance.close()'
        )
        second = subprocess.run([sys.executable, '-c', code],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(second.stdout.strip(), 'False')
        self.assertTrue(first.restore_requested())
        self.assertFalse(first.restore_requested())

        first.close()
        third = SingleInstance(name)
        self.addCleanup(third.close)
        self.assertTrue(third.acquired)


if __name__ == '__main__':
    unittest.main()
