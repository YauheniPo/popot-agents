"""Concurrent variants must preserve capture ownership and baseline ordering."""
import json
import os
from pathlib import Path
import signal
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from evals import save
from evals.matrix import run
from tests.test_eval_comparison import experiment
from tests.test_eval_settings import unified_settings


def configuration():
    return {**unified_settings(), 'parallel_models': 2, 'baseline': 'a', 'variants': [
        {'name': n, 'model': 'provider/' + n, 'parameters': {}} for n in ('a', 'b', 'c')]}


class ParallelEvalTests(unittest.TestCase):
    def test_terminal_interrupt_waits_for_launcher_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            child = directory / 'child.py'
            child.write_text(
                'import signal,time,sys\nfrom pathlib import Path\n'
                'root=Path(sys.argv[1])\n'
                'def interrupted(*args):\n'
                ' (root/"interrupted").touch();sys.exit(7)\n'
                'signal.signal(signal.SIGINT,interrupted)\n'
                '(root/"started").touch()\n'
                'while not (root/"cancel").exists():time.sleep(.01)\n'
                '(root/"cleaned").touch()\n')
            coordinator = (
                'import os,sys\nfrom pathlib import Path\n'
                'from concurrent.futures import ThreadPoolExecutor\n'
                'from evals.matrix import _launch\n'
                'root=Path(sys.argv[1])\n'
                'with ThreadPoolExecutor(max_workers=1) as pool:\n'
                ' future=pool.submit(_launch,{"command":[sys.executable,str(root/"child.py"),str(root)],'
                '"log":root/"launcher.log","env":os.environ.copy()})\n'
                ' try:future.result()\n'
                ' except KeyboardInterrupt:\n'
                '  (root/"cancel").touch();future.result()\n')
            process = subprocess.Popen([sys.executable, '-c', coordinator, str(directory)],
                                       start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not (directory / 'started').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((directory / 'started').exists())
                os.killpg(process.pid, signal.SIGINT)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertTrue((directory / 'cleaned').exists())
                self.assertFalse((directory / 'interrupted').exists())
            finally:
                if process.poll() is None:
                    (directory / 'cancel').touch()
                    process.kill()
                    process.communicate(timeout=5)

    def test_matrix_uses_frozen_launcher_when_original_file_changes(self):
        from evals.matrix import ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repository'
            shutil.copytree(ROOT / 'evals', root / 'evals', ignore=shutil.ignore_patterns('__pycache__'))
            original = (root / 'evals/run-all.sh').read_text()
            config = configuration()
            config['variants'] = config['variants'][:1]

            def launch(command, **kwargs):
                self.assertNotEqual(Path(command[1]), root / 'evals/run-all.sh')
                self.assertEqual(Path(command[1]).read_text(), original)
                (root / 'evals/run-all.sh').write_text('"')
                for key, value in experiment().items():
                    save(Path(kwargs['env']['POPOT_EVAL_OUTPUT']) / 'baseline' / (key + '.json'), value)

            with patch('evals.matrix.ROOT', root), patch('evals.matrix.subprocess.run', side_effect=launch):
                result = run(Path(tmp) / 'matrix', config)
            self.assertEqual(result['status'], 'completed')

    def test_cancellation_stops_queued_variants_and_waits_for_active_cleanup(self):
        started = threading.Barrier(3, timeout=3)
        stop = threading.Event()
        finished = set()

        def launch(command, **kwargs):
            name = Path(kwargs['env']['POPOT_EVAL_OUTPUT']).name
            started.wait()
            self.assertTrue(stop.wait(3))
            finished.add(name)
            raise subprocess.CalledProcessError(130, command)

        def interrupt(*args, **kwargs):
            started.wait()
            raise KeyboardInterrupt

        def cancel(jobs):
            self.assertEqual({j['name'] for j in jobs}, {'a', 'b'})
            stop.set()
            return []

        with tempfile.TemporaryDirectory() as tmp, \
                patch('evals.matrix.subprocess.run', side_effect=launch), \
                patch('evals.matrix.wait', side_effect=interrupt), \
                patch('evals.matrix._cancel', side_effect=cancel):
            output = Path(tmp) / 'matrix'
            settings = configuration()
            with self.assertRaises(KeyboardInterrupt):
                run(output, settings)
            result = json.loads((output / 'matrix.json').read_text())
        self.assertEqual(finished, {'a', 'b'})
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['active_variants'], [])

    def test_execution_parameters_expose_concurrency_changes(self):
        from evals.compare import parameters
        self.assertEqual(parameters({})['parallel_models'], 1)
        self.assertEqual(parameters({'evals': {'parallel_models': 3}})['parallel_models'], 3)

    def test_variants_overlap_with_bounded_capacity_and_compare_after_reference(self):
        both = threading.Barrier(2, timeout=3)
        candidate_finished = threading.Event()
        lock = threading.Lock()
        active, peak, finished = set(), [], set()
        coordinator = threading.get_ident()
        comparisons = []

        def record(path, value):
            if path.name == 'matrix.json':
                self.assertEqual(threading.get_ident(), coordinator)
            save(path, value)

        def launch(command, **kwargs):
            name = Path(kwargs['env']['POPOT_EVAL_OUTPUT']).name
            with lock:
                active.add(name)
                peak.append(len(active))
            if name in {'a', 'b'}:
                both.wait()
                if name == 'a':
                    self.assertTrue(candidate_finished.wait(3))
            directory = Path(kwargs['env']['POPOT_EVAL_OUTPUT']) / 'baseline'
            for key, value in experiment().items():
                save(directory / (key + '.json'), value)
            if name == 'b':
                candidate_finished.set()
            with lock:
                active.remove(name)
                finished.add(name)

        def compare(*args):
            self.assertIn('a', finished)
            comparisons.append(True)
            return {'ok': True}

        with tempfile.TemporaryDirectory() as tmp, \
                patch('evals.matrix.subprocess.run', side_effect=launch), \
                patch('evals.matrix.save', side_effect=record), \
                patch('evals.metrics.publish_comparison', side_effect=compare):
            result = run(Path(tmp) / 'matrix', configuration())
        self.assertEqual(max(peak), 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(set(result['runs']), {'a', 'b', 'c'})
        self.assertEqual(len(comparisons), 2)

    def test_one_failed_variant_keeps_other_completed_measurements(self):
        def launch(command, **kwargs):
            name = Path(kwargs['env']['POPOT_EVAL_OUTPUT']).name
            if name == 'b':
                raise subprocess.CalledProcessError(7, command)
            for key, value in experiment().items():
                save(Path(kwargs['env']['POPOT_EVAL_OUTPUT']) / 'baseline' / (key + '.json'), value)

        with tempfile.TemporaryDirectory() as tmp, \
                patch('evals.matrix.subprocess.run', side_effect=launch), \
                patch('evals.metrics.publish_comparison', return_value={'ok': True}):
            output = Path(tmp) / 'matrix'
            settings = configuration()
            with self.assertRaises(subprocess.CalledProcessError):
                run(output, settings)
            result = json.loads((output / 'matrix.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(set(result['runs']), {'a', 'c'})
        self.assertEqual(result['failures']['b']['exit_code'], 7)
