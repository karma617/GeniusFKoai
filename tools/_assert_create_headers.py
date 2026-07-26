#!/usr/bin/env python3
"""Ensure latest create_account header builder no longer forces oai-device-id."""
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path('.').resolve()))
from platforms.chatgpt.register import RegistrationEngine, ProtocolFingerprint

class DummyEmail:
    service_type = type('T', (), {'value': 'dummy'})()

engine = RegistrationEngine(email_service=DummyEmail())
engine._device_id = '15a3b48e-c795-4c96-a999-ab526a97901d'
engine.protocol_fingerprint = ProtocolFingerprint.create()
headers = engine._latest_chatgpt_json_headers(referer='https://auth.openai.com/about-you')
assert 'oai-device-id' not in headers, headers
assert headers['x-access-flow-invocation-id']
assert headers['accept'] == 'application/json'
assert headers['user-agent'].startswith('Mozilla/5.0 (Macintosh')
assert 'sec-ch-ua' not in headers  # Firefox no client hints
print('create/json headers OK', sorted(headers.keys()))

# chatgpt backend headers still include oai-device-id
h2 = engine._latest_chatgpt_chatgpt_client_headers(target_path='/backend-anon/me')
assert h2['oai-device-id'] == engine._device_id
assert h2['oai-client-version'].startswith('prod-2c08737')
assert h2['oai-client-build-number'] == '8578659'
print('chatgpt client headers OK')
