import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from config import load_config


class EnvTests(unittest.TestCase):
    def test_quotes_comments_and_literal_password(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / '.env'
            path.write_text('export GEMINI_API_KEY=test-key # comment\nX_PASSWORD="a#b$(literal)"\n')
            self.assertEqual(load_config(path), {'GEMINI_API_KEY': 'test-key', 'X_PASSWORD': 'a#b$(literal)'})

    def test_shell_environment_overrides_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GEMINI_API_KEY': 'shell'}, clear=True):
            path = Path(folder) / '.env'
            path.write_text('GEMINI_API_KEY=file\n')
            self.assertEqual(load_config(path)['GEMINI_API_KEY'], 'shell')

    def test_environment_works_without_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GEMINI_API_KEY': 'shell'}, clear=True):
            self.assertEqual(load_config(Path(folder) / '.env')['GEMINI_API_KEY'], 'shell')
