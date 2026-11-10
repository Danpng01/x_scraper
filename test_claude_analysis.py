import json
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
import claude_analysis as claude
import gemini_analysis as engine
import x_stock_ideas as core
from test_x_stock_ideas import QueueOpener, Response
from test_gemini_analysis import http_error

class ClaudeTests(unittest.TestCase):
    def test_gateway_request_and_response(self):
        reply={'stop_reason':'end_turn','content':[{'type':'text','text':'{"items":[]}'}], 'usage':{'input_tokens':100,'output_tokens':20}}
        opener=QueueOpener(Response(reply))
        db=core.open_db(':memory:'); engine.init_tables(db)
        try:
            client=claude.ClaudeClient('secret', 'https://example.com/claude', 'claude-sonnet-5-5',opener=opener,db=db)
            self.assertEqual(client.generate(engine.make_request([],[])),{'items':[]})
            req=opener.calls[0]; body=json.loads(req.data)
            self.assertEqual(req.full_url,'https://example.com/claude/v1/messages')
            self.assertEqual(req.get_header('X-api-key'),'secret')
            self.assertNotIn('secret',req.full_url)
            self.assertEqual(body['model'],'claude-sonnet-5-5')
            self.assertIn('messages',body)
            self.assertEqual(db.execute('SELECT total_tokens FROM gemini_usage').fetchone()[0],120)
        finally: db.close()

    def test_v1_suffix_is_not_duplicated(self):
        client=claude.ClaudeClient('secret','https://example.com/v1')
        self.assertEqual(client.url,'https://example.com/v1/messages')

    def test_quota_stops_without_switching_provider(self):
        opener=QueueOpener(http_error(429,{'error':{'message':'quota exhausted'}}))
        client=claude.ClaudeClient('secret',opener=opener)
        with self.assertRaises(core.PauseCollection): client.generate(engine.make_request([],[]))
        self.assertEqual(client.requests,1)

    def test_settings_match_reference_package(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'.env'
            path.write_text('ANTHROPIC_AUTH_TOKEN=abc\nANTHROPIC_BASE_URL=https://example.com/claude\nANTHROPIC_MODEL=custom-model\n')
            self.assertEqual(claude.load_settings(path),('abc','https://example.com/claude','custom-model'))

    def test_http_endpoint_is_rejected(self):
        with self.assertRaises(core.CollectorError): claude.ClaudeClient('secret','http://example.com')
